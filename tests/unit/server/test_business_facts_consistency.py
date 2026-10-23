"""业务事实在重放、事务失败和 SQLite 外键开关下保持同一执行语义。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.db.models import ExecuteRecord
from axile.server.db.models.analysis import analysis_state
from axile.server.db.models.analysis_replay import replay_observation
from axile.server.db.models.analysis_source import source_execution
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_initial_migration import _MIGRATIONS_DIR, _load_migration

_CREATED = "2026-10-20T09:30:00.000001"


def _fact_tables():
    """使用公开的域表清单，检查完整事实图而非猜测表名前缀。"""
    from axile.server.business_facts.repository import execution_fact_tables

    return execution_fact_tables()


def _input(value=0.25):
    return {"channel_type": "CTP", "curr_target": {"A": value}, "last_target": {}}


def _result(*, inputs=None):
    payload = {
        "status": "SUCCEEDED",
        "success": True,
        "execution_time": 0.125,
        "account_assets": {
            "total_asset": 1000.0,
            "available_cash": 800.0,
            "market_value": 200.0,
            "currency": "CNY",
            "positions": [{"symbol": "A", "volume": 2.0, "direction": "LONG"}],
        },
        "symbol_results": {
            "A": {
                "status": "SUCCEEDED",
                "sizing": {"unit_multiplier": 1, "target_quantity": 2},
                "first_tick": {"last_price": 100.0},
                "orders": [{"order_id": "order-A", "symbol": "A", "direction": "BUY", "volume": 2, "price": 100}],
                "trades": [
                    {
                        "trade_id": "trade-A",
                        "order_id": "order-A",
                        "symbol": "A",
                        "trade_price": 100.0,
                        "trade_volume": 2.0,
                        "trade_time": "2026-10-20T09:30:01.123456",
                        "trade_value": 200.0,
                        "extra": {"commission": 0, "commission_asset": "CNY"},
                    }
                ],
            }
        },
    }
    if inputs is not None:
        payload["inputs"] = inputs
    return payload


def _summary_content(result):
    """使用真实 worker 的正文结构，另保留独立于业务结果的审计附注。"""
    from axile.server.execution.execution_summaries import (
        build_execution_outcome_details,
        build_execution_summary_from_symbol_results,
        build_symbol_reconciliation,
    )

    return {
        **build_execution_outcome_details(result),
        "summary": build_execution_summary_from_symbol_results(result),
        "success": result.get("success", True),
        "execution_time": result.get("execution_time"),
        "reconciliation": build_symbol_reconciliation(result, None),
        "independent_evidence": {"review_note": "retain journal after history deletion", "observed_zero": 0},
    }


def _wire_json(value):
    """比较原始线上的字段顺序与数值表示，不对键排序或补默认值。"""
    import json

    return json.dumps(
        business_evidence(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


async def _store_summary(session, *, execution_id="facts-one", account_id=1):
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.business_facts.artifacts import compact_execution_summary
    from axile.server.db.models import ExecutionArtifact

    result = _result()
    original = _summary_content(result)
    record = await _store(session, execution_id=execution_id, account_id=account_id, result=result)
    artifact = ExecutionArtifact(
        execution_id=execution_id,
        artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
        schema_version=2,
        content=deepcopy(original),
    )
    session.add(artifact)
    await session.flush()
    await compact_execution_summary(session, record, result=result)
    await session.flush()
    return record, artifact, original


@asynccontextmanager
async def _database(tmp_path, *, foreign_keys=False):
    """真实 SQLite 触发器也必须在未启用外键的连接上保持业务约束。"""
    tables = _fact_tables()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fact-consistency.db'}")

    @sa.event.listens_for(engine.sync_engine, "connect")
    def configure(dbapi, _record):
        dbapi.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
            header_migration = _load_migration(_MIGRATIONS_DIR / "0012_performance_snapshots.py")
            await connection.run_sync(header_migration.install_triggers)
            facts_migration = _load_migration(_MIGRATIONS_DIR / "0037_business_execution_facts.py")
            await connection.run_sync(facts_migration.install_revision_triggers)
        async with sessions() as session:
            session.add_all(
                [
                    build_account(id=1, name="facts-one", portfolio_id=None),
                    build_account(id=2, name="facts-two", portfolio_id=None),
                ]
            )
            await session.flush()
            await session.execute(analysis_state.insert(), [{"account_id": 1}, {"account_id": 2}])
            await session.commit()
        yield engine, sessions, tables
    finally:
        await engine.dispose()


async def _store(session, *, execution_id="facts-one", account_id=1, payload=None, result=None):
    from axile.server.business_facts.repository import store_execution_facts

    record = ExecuteRecord(
        account_id=account_id,
        execution_id=execution_id,
        created_at=_CREATED,
        is_success=1,
    )
    session.add(record)
    await session.flush()
    await store_execution_facts(
        session, record, _input() if payload is None else payload, _result() if result is None else result
    )
    await session.flush()
    return record


async def _counts(session, tables):
    return {
        table.name: (await session.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()
        for table in tables
    }


async def _version(session, account_id=1):
    return (
        await session.execute(
            sa.select(analysis_state.c.source_version).where(analysis_state.c.account_id == account_id)
        )
    ).scalar_one()


async def _project(session, record):
    from axile.server.analysis_history import read_analysis_record
    from axile.server.analysis_observations import write_observation_fact
    from axile.server.analysis_source import write_source_facts

    source = await read_analysis_record(session, record)
    await write_observation_fact(session, source)
    assert await write_source_facts(session, source)


async def _projection_rows(session, record_id):
    from axile.server.db.models.analysis_replay import replay_observation_symbol
    from axile.server.db.models.analysis_source import SOURCE_FACT_TABLES

    return {
        table.name: list((await session.execute(sa.select(table).where(table.c.record_id == record_id))).mappings())
        for table in (*SOURCE_FACT_TABLES, replay_observation, replay_observation_symbol)
    }


def test_duplicate_terminal_write_keeps_first_facts_and_versions(tmp_path):
    """成功与终止两条收尾路径重放时，首份终态及其事实图保持不变。"""
    from axile.server.business_facts.repository import read_execution_result
    from axile.server.execution.records import _persist_execute_record

    async def check():
        async with _database(tmp_path) as (_, sessions, tables):
            first_result = {"task_status": "TERMINATED", "error": "operator stopped", "status": "FAILED"}
            first = await _persist_execute_record(
                account_id=1,
                execution_id="first-terminal-wins",
                raw_input=_input(),
                raw_result=first_result,
                is_success=0,
                session_factory=sessions,
            )
            async with sessions() as session:
                original_counts = await _counts(session, tables)
                original_version = await _version(session)
            second = await _persist_execute_record(
                account_id=1,
                execution_id="first-terminal-wins",
                raw_input=_input(0.75),
                raw_result=_result(),
                is_success=1,
                session_factory=sessions,
            )
            async with sessions() as session:
                assert business_evidence(first.id) == business_evidence(second.id)
                assert business_evidence(second.is_success) == business_evidence(0)
                assert business_evidence(await read_execution_result(session, second)) == business_evidence(
                    first_result
                )
                assert business_evidence(await _counts(session, tables)) == business_evidence(original_counts)
                assert business_evidence(await _version(session)) == business_evidence(original_version)

    asyncio.run(check())


def test_legacy_null_execution_ids_keep_independent_record_and_input_identity(tmp_path):
    """两个没有执行标识的历史记录不能因相同输入被错误合并。"""
    from axile.server.business_facts.repository import read_execution_input

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                first = await _store(session, execution_id=None)
                second = await _store(session, execution_id=None)
                await session.commit()
            async with sessions() as session:
                rows = (await session.execute(sa.select(ExecuteRecord))).scalars().all()
                assert business_evidence(len(rows)) == business_evidence(2)
                assert all(row.execution_id is None for row in rows)
                assert first.id != second.id and first.input_id != second.input_id
                assert business_evidence(await read_execution_input(session, first.input_id)) == business_evidence(
                    _input()
                )
                assert business_evidence(await read_execution_input(session, second.input_id)) == business_evidence(
                    _input()
                )

    asyncio.run(check())


@pytest.mark.parametrize("different", [False, True], ids=["shared-input", "distinct-inputs"])
def test_input_and_result_input_share_only_equal_payloads(tmp_path, different):
    """结果中的输入可以不同，不能用头输入覆盖它或制造第二份相同事实。"""
    from axile.server.business_facts.repository import read_execution_input, read_execution_result

    payload = _input()
    result_input = _input(0.5) if different else deepcopy(payload)

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                record = await _store(session, payload=payload, result=_result(inputs=result_input))
                await session.commit()
            async with sessions() as session:
                assert (record.input_id != record.result_input_id) is different
                assert business_evidence(await read_execution_input(session, record.input_id)) == business_evidence(
                    payload
                )
                assert business_evidence(
                    await read_execution_input(session, record.result_input_id)
                ) == business_evidence(result_input)
                assert business_evidence((await read_execution_result(session, record))["inputs"]) == business_evidence(
                    result_input
                )

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
@pytest.mark.parametrize("reference", ["input_id", "result_input_id"])
def test_cross_account_input_references_are_rejected_by_database(tmp_path, foreign_keys, reference):
    """直接写表也不能把其他账户的输入挂到当前执行，外键关闭时同样如此。"""
    from axile.server.business_facts.repository import persist_execution_input

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                borrowed = await persist_execution_input(
                    session, account_id=1, execution_id="before-only", payload=_input(), created_at=_CREATED
                )
                await session.commit()
            async with sessions() as session:
                with pytest.raises(IntegrityError):
                    await session.execute(
                        sa.insert(ExecuteRecord).values(
                            account_id=2,
                            execution_id="cross-account",
                            created_at=_CREATED,
                            is_success=1,
                            **{reference: borrowed},
                        )
                    )
                await session.rollback()
                assert business_evidence(
                    (await session.execute(sa.select(sa.func.count()).select_from(ExecuteRecord))).scalar_one()
                ) == business_evidence(0)

    asyncio.run(check())


def test_result_write_failure_preserves_committed_preorder_input(tmp_path):
    """结果子行失败必须撤回整份终态事实，已提交的下单前输入仍可恢复。"""
    from axile.server.business_facts.repository import persist_execution_input, read_execution_input
    from axile.server.business_facts.trades import TABLES as trade_tables

    async def check():
        async with _database(tmp_path) as (engine, sessions, tables):
            async with sessions() as session:
                input_id = await persist_execution_input(
                    session,
                    account_id=1,
                    execution_id="facts-one",
                    payload=_input(),
                    created_at=_CREATED,
                    before_assets=_result()["account_assets"],
                )
                await session.commit()
                original_counts = await _counts(session, tables)
                original_version = await _version(session)

            def reject_trade(_connection, _cursor, statement, _parameters, _context, _many):
                if any(statement.startswith(f"INSERT INTO {table.name} ") for table in trade_tables):
                    raise RuntimeError("synthetic final-result storage failure")

            sa.event.listen(engine.sync_engine, "before_cursor_execute", reject_trade)
            try:
                async with sessions() as session:
                    with pytest.raises(RuntimeError, match="synthetic final-result"):
                        await _store(session)
                    await session.rollback()
            finally:
                sa.event.remove(engine.sync_engine, "before_cursor_execute", reject_trade)
            async with sessions() as session:
                assert business_evidence(await _counts(session, tables)) == business_evidence(original_counts)
                assert business_evidence(await _version(session)) == business_evidence(original_version)
                assert business_evidence(await read_execution_input(session, input_id)) == business_evidence(_input())
                assert business_evidence(
                    (await session.execute(sa.select(sa.func.count()).select_from(ExecuteRecord))).scalar_one()
                ) == business_evidence(0)

    asyncio.run(check())


def test_preorder_replay_preserves_identity_time_and_before_assets(tmp_path):
    """相同重放复用已冻结输入及前置资产，冲突重放不得覆盖原证据。"""
    from axile.server.business_facts.assets import asset_snapshot
    from axile.server.business_facts.inputs import execution_input
    from axile.server.business_facts.repository import persist_execution_input, read_execution_input

    async def check():
        async with _database(tmp_path) as (_, sessions, tables):
            async with sessions() as session:
                first = await persist_execution_input(
                    session,
                    account_id=1,
                    execution_id="before-replay",
                    payload=_input(),
                    created_at=_CREATED,
                    before_assets=_result()["account_assets"],
                )
                await session.commit()
                original_counts = await _counts(session, tables)
                original_version = await _version(session)
            async with sessions() as session:
                replay = await persist_execution_input(
                    session,
                    account_id=1,
                    execution_id="before-replay",
                    payload=_input(),
                    created_at="2026-10-20T10:00:00",
                    before_assets={"total_asset": 9999},
                )
                await session.commit()
                assert business_evidence(replay) == business_evidence(first)
                assert business_evidence(await _counts(session, tables)) == business_evidence(original_counts)
                assert business_evidence(await _version(session)) == business_evidence(original_version)
                assert business_evidence(
                    (await session.execute(sa.select(execution_input.c.created_at))).scalar_one()
                ) == business_evidence(_CREATED)
                assert business_evidence(
                    (await session.execute(sa.select(asset_snapshot.c.total_asset))).scalar_one()
                ) == business_evidence(1000.0)
            async with sessions() as session:
                with pytest.raises(ValueError):
                    await persist_execution_input(
                        session,
                        account_id=1,
                        execution_id="before-replay",
                        payload=_input(0.75),
                        created_at=_CREATED,
                    )
                await session.rollback()
                assert business_evidence(await read_execution_input(session, first)) == business_evidence(_input())
                assert business_evidence(await _counts(session, tables)) == business_evidence(original_counts)

    asyncio.run(check())


@pytest.mark.parametrize("commit", [False, True], ids=["rollback", "commit"])
def test_child_change_invalidation_and_source_version_are_atomic(tmp_path, commit):
    """同数量成交的数值修改也应失效；事务回滚同时恢复投影、值与版本。"""
    from axile.server.business_facts.trades import TABLES as trade_tables

    trade = next(table for table in trade_tables if "trade_price" in table.c)

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                record = await _store(session)
                await _project(session, record)
                await session.commit()
                original_version = await _version(session)
            async with sessions() as session:
                await session.execute(trade.update().where(trade.c.record_id == record.id).values(trade_price=101.0))
                assert await _version(session) > original_version
                for projection in (source_execution, replay_observation):
                    assert business_evidence(
                        (
                            await session.execute(
                                sa.select(sa.func.count())
                                .select_from(projection)
                                .where(projection.c.record_id == record.id)
                            )
                        ).scalar_one()
                    ) == business_evidence(0)
                await session.commit() if commit else await session.rollback()
            async with sessions() as session:
                assert (await _version(session) > original_version) is commit
                assert business_evidence(
                    (await session.execute(sa.select(trade.c.trade_price))).scalar_one()
                ) == business_evidence(101.0 if commit else 100.0)
                for projection in (source_execution, replay_observation):
                    assert business_evidence(
                        (
                            await session.execute(
                                sa.select(sa.func.count())
                                .select_from(projection)
                                .where(projection.c.record_id == record.id)
                            )
                        ).scalar_one()
                    ) == business_evidence(0 if commit else 1)

    asyncio.run(check())


def test_result_input_target_mutation_invalidates_its_execution(tmp_path):
    """仅通过 result_input_id 关联的目标也属于执行事实，不能漏掉失效。"""
    from axile.server.business_facts.inputs import execution_input_target

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                record = await _store(session, result=_result(inputs=_input(0.5)))
                assert record.input_id != record.result_input_id
                await _project(session, record)
                await session.commit()
                original_version = await _version(session)
            async with sessions() as session:
                await session.execute(
                    execution_input_target.update()
                    .where(execution_input_target.c.input_id == record.result_input_id)
                    .where(execution_input_target.c.role == "curr_target")
                    .values(value=0.75)
                )
                await session.commit()
            async with sessions() as session:
                assert await _version(session) > original_version
                for projection in (source_execution, replay_observation):
                    assert business_evidence(
                        (await session.execute(sa.select(sa.func.count()).select_from(projection))).scalar_one()
                    ) == business_evidence(0)

    asyncio.run(check())


def test_target_reparenting_invalidates_both_accounts(tmp_path):
    """子行改变归属时，旧账户和新账户都不能继续使用之前发布的源投影。"""
    from axile.server.business_facts.inputs import execution_input, execution_input_target

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                first = await _store(session)
                second = await _store(session, execution_id="facts-two", account_id=2, payload=_input(0.5))
                await session.execute(
                    execution_input_target.delete().where(execution_input_target.c.input_id == second.input_id)
                )
                await session.execute(
                    execution_input.update().where(execution_input.c.id == second.input_id).values(curr_target_count=0)
                )
                await _project(session, first)
                await _project(session, second)
                await session.commit()
                before = {account_id: await _version(session, account_id) for account_id in (1, 2)}
            async with sessions() as session:
                await session.execute(
                    execution_input_target.update()
                    .where(execution_input_target.c.input_id == first.input_id)
                    .where(execution_input_target.c.role == "curr_target")
                    .values(input_id=second.input_id)
                )
                await session.commit()
            async with sessions() as session:
                for account_id in (1, 2):
                    assert await _version(session, account_id) > before[account_id]
                for projection in (source_execution, replay_observation):
                    assert business_evidence(
                        (await session.execute(sa.select(sa.func.count()).select_from(projection))).scalar_one()
                    ) == business_evidence(0)

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_referenced_input_cannot_be_moved_to_another_account(tmp_path, foreign_keys):
    """已被终态引用的输入不能先改账户来绕过执行头上的跨账户校验。"""
    from axile.server.business_facts.inputs import execution_input

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                record = await _store(session)
                await session.commit()
            async with sessions() as session:
                with pytest.raises(IntegrityError):
                    await session.execute(
                        execution_input.update().where(execution_input.c.id == record.input_id).values(account_id=2)
                    )
                await session.rollback()
                assert business_evidence(
                    (
                        await session.execute(
                            sa.select(execution_input.c.account_id).where(execution_input.c.id == record.input_id)
                        )
                    ).scalar_one()
                ) == business_evidence(1)

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_record_delete_removes_owned_facts_and_unreferenced_inputs(tmp_path, foreign_keys):
    """删除执行应清理两份输入及各域子行，独立的待执行输入仍保留。"""
    from axile.server.business_facts.inputs import execution_input
    from axile.server.business_facts.repository import persist_execution_input, read_execution_input

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, tables):
            async with sessions() as session:
                pending = await persist_execution_input(
                    session, account_id=1, execution_id="pending", payload=_input(0), created_at=_CREATED
                )
                record = await _store(session, result=_result(inputs=_input(0.5)))
                await _project(session, record)
                await session.commit()
            async with sessions() as session:
                await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.id == record.id))
                await session.commit()
            async with sessions() as session:
                assert business_evidence(
                    (await session.execute(sa.select(execution_input.c.id))).scalars().all()
                ) == business_evidence([pending])
                assert business_evidence(await read_execution_input(session, pending)) == business_evidence(_input(0))
                for table in (*tables, source_execution, replay_observation):
                    if "record_id" in table.c:
                        assert business_evidence(
                            (await session.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()
                        ) == business_evidence(0), table.name
                    if "input_id" in table.c:
                        assert not set((await session.execute(sa.select(table.c.input_id))).scalars()) - {pending}
                    if "asset_id" in table.c:
                        assert business_evidence(
                            (await session.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()
                        ) == business_evidence(0), table.name

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_delete_keeps_input_until_its_last_reference_is_removed(tmp_path, foreign_keys):
    """另一执行的结果输入仍引用同一证据时，删除第一执行不能提前级联删除它。"""
    from axile.server.business_facts.inputs import execution_input
    from axile.server.business_facts.repository import read_execution_input

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                first = await _store(session)
                second = await _store(session, execution_id="facts-two", payload=_input(0.5))
                await session.execute(
                    sa.update(ExecuteRecord).where(ExecuteRecord.id == second.id).values(result_input_id=first.input_id)
                )
                await session.commit()
            async with sessions() as session:
                await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.id == first.id))
                await session.commit()
            async with sessions() as session:
                assert business_evidence(await read_execution_input(session, first.input_id)) == business_evidence(
                    _input()
                )
                assert business_evidence(await read_execution_input(session, second.input_id)) == business_evidence(
                    _input(0.5)
                )
                await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.id == second.id))
                await session.commit()
            async with sessions() as session:
                assert business_evidence(
                    (await session.execute(sa.select(sa.func.count()).select_from(execution_input))).scalar_one()
                ) == business_evidence(0)

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_terminal_delete_retains_immutable_input_artifact_and_before_assets(tmp_path, foreign_keys):
    """终态行删除后，前置审计附件仍能解析原输入及其资产图。"""
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.business_facts.artifacts import make_artifact_reference, resolve_artifact_content
    from axile.server.business_facts.assets import asset_position, asset_snapshot
    from axile.server.business_facts.repository import persist_execution_input
    from axile.server.db.models import ExecutionArtifact

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                input_id = await persist_execution_input(
                    session,
                    account_id=1,
                    execution_id="facts-one",
                    payload=_input(),
                    created_at=_CREATED,
                    before_assets=_result()["account_assets"],
                )
                artifact = ExecutionArtifact(
                    execution_id="facts-one",
                    artifact_type=ExecutionArtifactType.STANDARD_INPUT,
                    schema_version=3,
                    content=make_artifact_reference(view="input", input_id=input_id),
                )
                session.add(artifact)
                record = await _store(session)
                await session.commit()
            async with sessions() as session:
                await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.id == record.id))
                await session.commit()
            async with sessions() as session:
                retained = await session.get(ExecutionArtifact, artifact.id)
                assert retained is not None
                assert business_evidence(
                    (await resolve_artifact_content(session, retained, account_id=1))["input"]
                ) == business_evidence(_input())
                before = (
                    (await session.execute(sa.select(asset_snapshot).where(asset_snapshot.c.input_id == input_id)))
                    .mappings()
                    .one()
                )
                assert before["scope"] == "before" and before["total_asset"] == 1000.0
                positions = (
                    (
                        await session.execute(
                            sa.select(asset_position).where(asset_position.c.asset_id == before["asset_id"])
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(positions) == 1 and positions[0]["symbol"] == "A" and positions[0]["volume"] == 2.0

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_terminal_delete_retains_asset_only_artifact_reference(tmp_path, foreign_keys):
    """仅携带资产标识的审计引用也应在删除终态后保留原观测及其输入锚点。"""
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.business_facts.artifacts import make_artifact_reference, resolve_artifact_content
    from axile.server.business_facts.assets import asset_binding, asset_snapshot
    from axile.server.db.models import ExecutionArtifact

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                record = await _store(session)
                asset_id = (
                    await session.execute(
                        sa.select(asset_binding.c.asset_id)
                        .where(asset_binding.c.record_id == record.id)
                        .where(asset_binding.c.path == "/account_assets")
                    )
                ).scalar_one()
                artifact = ExecutionArtifact(
                    execution_id="facts-one",
                    artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT,
                    schema_version=3,
                    content=make_artifact_reference(view="assets_after", asset_id=asset_id),
                )
                session.add(artifact)
                await session.commit()
            async with sessions() as session:
                await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.id == record.id))
                await session.commit()
            async with sessions() as session:
                retained = await session.get(ExecutionArtifact, artifact.id)
                assert retained is not None
                assert business_evidence(
                    (await resolve_artifact_content(session, retained, account_id=1))["account_assets"]
                ) == business_evidence(_result()["account_assets"])
                row = (
                    (await session.execute(sa.select(asset_snapshot).where(asset_snapshot.c.asset_id == asset_id)))
                    .mappings()
                    .one()
                )
                assert row["record_id"] is None and row["input_id"] == record.input_id

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_account_delete_cleans_its_pending_and_final_graph_only(tmp_path, foreign_keys):
    """账户删除清理其待执行输入及终态事实，另一个账户的详情保持完整。"""
    from axile.server.business_facts.inputs import execution_input
    from axile.server.business_facts.repository import persist_execution_input, read_execution_result
    from axile.server.db.models import Account

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, tables):
            async with sessions() as session:
                await persist_execution_input(
                    session,
                    account_id=1,
                    execution_id="never-finalized",
                    payload=_input(0),
                    created_at=_CREATED,
                    before_assets=_result()["account_assets"],
                )
                first = await _store(session)
                second = await _store(session, execution_id="facts-two", account_id=2)
                await session.commit()
            async with sessions() as session:
                await session.execute(sa.delete(Account).where(Account.id == 1))
                await session.commit()
            async with sessions() as session:
                assert await session.get(ExecuteRecord, first.id) is None
                assert business_evidence(await read_execution_result(session, second)) == business_evidence(_result())
                assert business_evidence(
                    set((await session.execute(sa.select(execution_input.c.account_id))).scalars())
                ) == business_evidence({2})
                for table in tables:
                    if "record_id" in table.c:
                        assert not set((await session.execute(sa.select(table.c.record_id))).scalars()) - {
                            second.id,
                            None,
                        }
                violations = (await session.execute(sa.text("PRAGMA foreign_key_check"))).all()
                fact_names = {table.name for table in tables}
                assert business_evidence([row for row in violations if row[0] in fact_names]) == business_evidence([])

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
@pytest.mark.parametrize("asset_account", [1, 2], ids=["other-execution", "other-account"])
def test_result_asset_binding_cannot_read_another_execution_observation(
    tmp_path, monkeypatch, foreign_keys, asset_account
):
    """合法外键指向其他执行的资产时，数据库或业务读取必须拒绝该关联。"""
    from axile.server.business_facts.assets import asset_binding
    from axile.server.business_facts.repository import persist_execution_input, read_execution_detail
    from axile.server.execution import input_persistence

    async def check():
        monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(tmp_path / "fact-consistency.db"))
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                await persist_execution_input(
                    session,
                    account_id=asset_account,
                    execution_id="unrelated-observation",
                    payload=_input(0.75),
                    created_at=_CREATED,
                )
                # 原生入口另开连接,需先提交冻结输入。
                await session.commit()
                reference = await input_persistence._persist_post_execute_assets(
                    account_id=asset_account,
                    execution_id="unrelated-observation",
                    payload={"account_assets": {"total_asset": 123456.0, "positions": []}},
                )
                assert reference is not None
                _, borrowed_asset_id = reference
                record = await _store(session)
                await session.commit()
            async with sessions() as session:
                try:
                    await session.execute(
                        asset_binding.update()
                        .where(asset_binding.c.record_id == record.id)
                        .where(asset_binding.c.path == "/account_assets")
                        .values(asset_id=borrowed_asset_id)
                    )
                except IntegrityError:
                    await session.rollback()
                else:
                    with pytest.raises(ValueError):
                        await read_execution_detail(session, record)
                    await session.rollback()

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
@pytest.mark.parametrize("asset_account", [1, 2], ids=["other-execution", "other-account"])
@pytest.mark.parametrize("include_positions", [False, True], ids=["scalar", "positions"])
def test_account_snapshot_cannot_read_another_execution_asset(
    tmp_path, monkeypatch, foreign_keys, asset_account, include_positions
):
    """资产观测引用被改为其他执行时，标量读取和持仓读取都必须拒绝。"""
    from axile.server.business_facts.assets import read_snapshot_assets, write_snapshot_assets
    from axile.server.business_facts.repository import persist_execution_input
    from axile.server.db.models import AccountAssetSnapshot
    from axile.server.execution import input_persistence

    async def check():
        monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(tmp_path / "fact-consistency.db"))
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                snapshot = AccountAssetSnapshot(
                    account_id=1, source="execution", execution_id="snapshot-local", created_at=_CREATED
                )
                await write_snapshot_assets(session, snapshot, _result()["account_assets"])
                await persist_execution_input(
                    session,
                    account_id=asset_account,
                    execution_id="unrelated-observation",
                    payload=_input(),
                    created_at=_CREATED,
                )
                # 原生入口另开连接,需先提交冻结输入。
                await session.commit()
                reference = await input_persistence._persist_post_execute_assets(
                    account_id=asset_account,
                    execution_id="unrelated-observation",
                    payload={"account_assets": {"total_asset": 123456.0, "positions": []}},
                )
                assert reference is not None
                _, borrowed_asset_id = reference
                await session.commit()
            async with sessions() as session:
                try:
                    await session.execute(
                        sa.update(AccountAssetSnapshot)
                        .where(AccountAssetSnapshot.id == snapshot.id)
                        .values(asset_fact_id=borrowed_asset_id)
                    )
                except IntegrityError:
                    await session.rollback()
                else:
                    reloaded = await session.get(AccountAssetSnapshot, snapshot.id)
                    assert reloaded is not None
                    with pytest.raises(ValueError):
                        await read_snapshot_assets(session, reloaded, include_positions=include_positions)
                    await session.rollback()

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_direct_terminal_delete_cannot_dangle_summary_reference(tmp_path, foreign_keys):
    """原始 SQL 删除不能让仍被独立审计摘要引用的终态结果消失。"""
    from axile.server.business_facts.artifacts import REFERENCE_KEY, resolve_artifact_content
    from axile.server.db.models import ExecutionArtifact

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                record, artifact, original = await _store_summary(session)
                await session.commit()
            async with sessions() as session:
                journal = await session.get(ExecutionArtifact, artifact.id)
                assert journal is not None
                assert business_evidence(journal.content[REFERENCE_KEY]["view"]) == business_evidence("result")
                assert business_evidence(journal.content[REFERENCE_KEY]["record_id"]) == business_evidence(record.id)
                assert journal.content[REFERENCE_KEY]["projection_version"] == 2
                with pytest.raises(IntegrityError):
                    await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.id == record.id))
                await session.rollback()
            async with sessions() as session:
                assert await session.get(ExecuteRecord, record.id) is not None
                journal = await session.get(ExecutionArtifact, artifact.id)
                assert journal is not None
                resolved = await resolve_artifact_content(session, journal, account_id=1)
                resolved.pop(REFERENCE_KEY)
                assert business_evidence(_wire_json(resolved)) == business_evidence(_wire_json(original))

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_application_history_delete_preserves_independent_summary_wire(tmp_path, foreign_keys):
    """应用删除先物化摘要，历史清理后完整审计正文仍能独立读取。"""
    from axile.server.api.routes.account_crud import delete_execute_records
    from axile.server.business_facts.artifacts import REFERENCE_KEY, resolve_artifact_content
    from axile.server.db.models import ExecutionArtifact

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                record, artifact, original = await _store_summary(session)
                other, other_artifact, other_original = await _store_summary(
                    session, execution_id="other-account-summary", account_id=2
                )
                await session.commit()
            async with sessions() as session:
                await delete_execute_records(session=session, account_id=1)
            async with sessions() as session:
                assert await session.get(ExecuteRecord, record.id) is None
                assert await session.get(ExecuteRecord, other.id) is not None
                journal = await session.get(ExecutionArtifact, artifact.id)
                assert journal is not None and journal.schema_version == 2
                assert REFERENCE_KEY not in journal.content
                assert business_evidence(_wire_json(journal.content)) == business_evidence(_wire_json(original))
                assert business_evidence(
                    _wire_json(await resolve_artifact_content(session, journal, account_id=1))
                ) == business_evidence(_wire_json(original))
                untouched = await session.get(ExecutionArtifact, other_artifact.id)
                assert untouched is not None and untouched.content[REFERENCE_KEY]["view"] == "result"
                resolved = await resolve_artifact_content(session, untouched, account_id=2)
                resolved.pop(REFERENCE_KEY)
                assert business_evidence(_wire_json(resolved)) == business_evidence(_wire_json(other_original))

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_detach_and_delete_rollback_restore_original_summary_reference(tmp_path, foreign_keys):
    """物化摘要与删除结果同属调用方事务；回滚同时恢复引用和事实图。"""
    from axile.server.business_facts.artifacts import (
        REFERENCE_KEY,
        detach_execution_summaries,
        resolve_artifact_content,
    )
    from axile.server.db.models import ExecutionArtifact

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, tables):
            async with sessions() as session:
                record, artifact, original = await _store_summary(session)
                await session.commit()
                before_counts = await _counts(session, tables)
                before_version = await _version(session)
            async with sessions() as session:
                await detach_execution_summaries(session, record)
                await session.execute(sa.delete(ExecuteRecord).where(ExecuteRecord.id == record.id))
                assert await session.get(ExecuteRecord, record.id) is None
                await session.rollback()
            async with sessions() as session:
                assert business_evidence(await _counts(session, tables)) == business_evidence(before_counts)
                assert business_evidence(await _version(session)) == business_evidence(before_version)
                journal = await session.get(ExecutionArtifact, artifact.id)
                assert journal is not None and journal.content[REFERENCE_KEY]["view"] == "result"
                resolved = await resolve_artifact_content(session, journal, account_id=1)
                resolved.pop(REFERENCE_KEY)
                assert business_evidence(_wire_json(resolved)) == business_evidence(_wire_json(original))

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_terminal_compaction_rollback_keeps_previously_committed_worker_summary(tmp_path, foreign_keys):
    """终态事务失败时，worker 已提交的完整摘要不能被替换为悬空引用。"""
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.business_facts.artifacts import REFERENCE_KEY, compact_execution_summary, resolve_artifact_content
    from axile.server.business_facts.repository import persist_execution_input, read_execution_input
    from axile.server.db.models import ExecutionArtifact

    result = _result()
    original = _summary_content(result)

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, tables):
            async with sessions() as session:
                input_id = await persist_execution_input(
                    session, account_id=1, execution_id="facts-one", payload=_input(), created_at=_CREATED
                )
                artifact = ExecutionArtifact(
                    execution_id="facts-one",
                    artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
                    schema_version=2,
                    content=deepcopy(original),
                )
                session.add(artifact)
                await session.commit()
                before_counts = await _counts(session, tables)
            async with sessions() as session:
                record = await _store(session, result=result)
                current = await session.scalar(
                    sa.select(ExecutionArtifact.content).where(ExecutionArtifact.id == artifact.id)
                )
                if REFERENCE_KEY not in current:
                    await compact_execution_summary(session, record, result=result)
                compacted = await session.scalar(
                    sa.select(ExecutionArtifact.content).where(ExecutionArtifact.id == artifact.id)
                )
                assert business_evidence(compacted[REFERENCE_KEY]["view"]) == business_evidence("result")
                await session.rollback()
            async with sessions() as session:
                assert business_evidence(
                    (await session.execute(sa.select(sa.func.count()).select_from(ExecuteRecord))).scalar_one()
                ) == business_evidence(0)
                assert business_evidence(await _counts(session, tables)) == business_evidence(before_counts)
                assert business_evidence(await read_execution_input(session, input_id)) == business_evidence(_input())
                journal = await session.get(ExecutionArtifact, artifact.id)
                assert journal is not None and journal.schema_version == 2 and REFERENCE_KEY not in journal.content
                assert business_evidence(
                    _wire_json(await resolve_artifact_content(session, journal, account_id=1))
                ) == business_evidence(_wire_json(original))

    asyncio.run(check())


@pytest.mark.parametrize(
    ("account_id", "execution_id"),
    [(2, "facts-one"), (1, "unrelated-execution")],
    ids=["wrong-account", "wrong-execution"],
)
def test_summary_reference_rejects_foreign_account_or_execution(tmp_path, account_id, execution_id):
    """即便结果编号和摘要校验值正确，也不能在其他账户或执行上下文中解析。"""
    from axile.server.business_facts.artifacts import REFERENCE_KEY, resolve_artifact_content
    from axile.server.db.models import ExecutionArtifact

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                _, artifact, _ = await _store_summary(session)
                await session.commit()
            async with sessions() as session:
                journal = await session.get(ExecutionArtifact, artifact.id)
                assert journal is not None and journal.content[REFERENCE_KEY]["view"] == "result"
                with pytest.raises(ValueError, match="账户"):
                    await resolve_artifact_content(
                        session,
                        {
                            "execution_id": execution_id,
                            "artifact_type": journal.artifact_type,
                            "content": journal.content,
                        },
                        account_id=account_id,
                    )

    asyncio.run(check())


@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
def test_summary_reference_freezes_child_until_journal_is_materialized(tmp_path, foreign_keys):
    """摘要引用期间禁止改成交；先物化审计后可修改业务事实，原审计保持原值。"""
    from axile.server.business_facts.artifacts import REFERENCE_KEY, detach_execution_summaries
    from axile.server.business_facts.repository import read_execution_result
    from axile.server.business_facts.trades import TABLES as trade_tables
    from axile.server.db.models import ExecutionArtifact

    trade = next(table for table in trade_tables if "trade_price" in table.c)

    async def check():
        async with _database(tmp_path, foreign_keys=foreign_keys) as (_, sessions, _):
            async with sessions() as session:
                record, artifact, original = await _store_summary(session)
                await session.commit()
            async with sessions() as session:
                with pytest.raises(IntegrityError):
                    await session.execute(
                        trade.update().where(trade.c.record_id == record.id).values(trade_price=101.0)
                    )
                await session.rollback()
            async with sessions() as session:
                await detach_execution_summaries(session, record)
                await session.execute(trade.update().where(trade.c.record_id == record.id).values(trade_price=101.0))
                await session.commit()
            async with sessions() as session:
                journal = await session.get(ExecutionArtifact, artifact.id)
                assert journal is not None and REFERENCE_KEY not in journal.content
                assert business_evidence(_wire_json(journal.content)) == business_evidence(_wire_json(original))
                updated = await read_execution_result(session, record)
                assert business_evidence(
                    updated["symbol_results"]["A"]["trades"][0]["trade_price"]
                ) == business_evidence(101.0)

    asyncio.run(check())


@pytest.mark.parametrize("second_account", [1, 2], ids=["same-account", "other-account"])
def test_composite_order_paths_cannot_invalidate_another_record(tmp_path, second_account):
    """不同执行可使用相同订单路径；新写入或修改只失效真正拥有该行的执行。"""
    from axile.server.business_facts.orders import order
    from axile.server.db.models.analysis_replay import replay_observation_symbol
    from axile.server.db.models.analysis_source import SOURCE_FACT_TABLES

    projections = (*SOURCE_FACT_TABLES, replay_observation, replay_observation_symbol)

    async def frozen_projection(session, record_id):
        return {
            table.name: list((await session.execute(sa.select(table).where(table.c.record_id == record_id))).mappings())
            for table in projections
        }

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                first = await _store(session)
                await _project(session, first)
                await session.commit()
                expected = await frozen_projection(session, first.id)
                first_version = await _version(session, 1)
                assert expected[source_execution.name] and expected[replay_observation.name]
            async with sessions() as session:
                second = await _store(session, execution_id="same-order-paths", account_id=second_account)
                await session.commit()
            async with sessions() as session:
                assert business_evidence(await frozen_projection(session, first.id)) == business_evidence(expected)
                if second_account != 1:
                    assert business_evidence(await _version(session, 1)) == business_evidence(first_version)
                await _project(session, second)
                await session.commit()
            async with sessions() as session:
                await session.execute(order.update().where(order.c.record_id == second.id).values(price=101.0))
                await session.commit()
            async with sessions() as session:
                assert business_evidence(await frozen_projection(session, first.id)) == business_evidence(expected)
                if second_account != 1:
                    assert business_evidence(await _version(session, 1)) == business_evidence(first_version)
                for table in (source_execution, replay_observation):
                    assert business_evidence(
                        (
                            await session.execute(
                                sa.select(sa.func.count()).select_from(table).where(table.c.record_id == second.id)
                            )
                        ).scalar_one()
                    ) == business_evidence(0)

    asyncio.run(check())


@pytest.mark.parametrize("new_snapshot", ["manual", "execution-index"])
@pytest.mark.parametrize("mutation", ["asset", "binding"])
def test_new_account_asset_observation_preserves_unrelated_record_projection(
    tmp_path, monkeypatch, new_snapshot, mutation
):
    """新账户观测只推动账户版本；真实执行资产或绑定变化才失效其拥有者。"""
    from axile.server.business_facts.assets import (
        asset_binding,
        asset_snapshot,
        ensure_execution_asset_snapshot,
        write_snapshot_assets,
    )
    from axile.server.db.models import AccountAssetSnapshot
    from axile.server.execution import input_persistence

    async def check():
        monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(tmp_path / "fact-consistency.db"))
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                first = await _store(session)
                alternate = _result()
                alternate["account_assets"]["total_asset"] = 1500.0
                # 原生入口另开连接,需先提交终态与冻结输入。
                await session.commit()
                after = await input_persistence._persist_post_execute_assets(
                    account_id=1, execution_id="facts-one", payload=alternate
                )
                assert after is not None
                _, alternate_asset_id = after
                await _project(session, first)
                await session.commit()
                original = await _projection_rows(session, first.id)
                version = await _version(session)
                assert original[source_execution.name] and original[replay_observation.name]
            second = None
            async with sessions() as session:
                if new_snapshot == "manual":
                    observation = AccountAssetSnapshot(account_id=1, source="manual", created_at=_CREATED)
                    await write_snapshot_assets(session, observation, {"total_asset": 2000.0, "positions": []})
                else:
                    second = await _store(session, execution_id="second-execution-index")
                    assert await ensure_execution_asset_snapshot(session, second) is not None
                await session.commit()
            async with sessions() as session:
                assert await _version(session) > version
                assert business_evidence(await _projection_rows(session, first.id)) == business_evidence(original)
                second_projection = None
                if second is not None:
                    await _project(session, second)
                    await session.commit()
                    second_projection = await _projection_rows(session, second.id)
            async with sessions() as session:
                if mutation == "asset":
                    bound = sa.select(asset_binding.c.asset_id).where(
                        asset_binding.c.record_id == first.id, asset_binding.c.path == "/account_assets"
                    )
                    await session.execute(
                        asset_snapshot.update().where(asset_snapshot.c.asset_id.in_(bound)).values(total_asset=1001.0)
                    )
                else:
                    await session.execute(
                        asset_binding.update()
                        .where(asset_binding.c.record_id == first.id)
                        .where(asset_binding.c.path == "/account_assets")
                        .values(asset_id=alternate_asset_id)
                    )
                await session.commit()
            async with sessions() as session:
                for table in (source_execution, replay_observation):
                    assert business_evidence(
                        (
                            await session.execute(
                                sa.select(sa.func.count()).select_from(table).where(table.c.record_id == first.id)
                            )
                        ).scalar_one()
                    ) == business_evidence(0)
                if second is not None:
                    assert business_evidence(await _projection_rows(session, second.id)) == business_evidence(
                        second_projection
                    )

    asyncio.run(check())


def test_atomic_new_graph_does_not_version_account_for_every_fill(tmp_path):
    """同一事务建立新执行时，源版本增量与订单成交行数无关。"""

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                start = await _version(session)
                await _store(session, execution_id="small")
                await session.commit()
                small = await _version(session)
                large_result = _result()
                large_result["symbol_results"]["A"]["orders"] *= 40
                large_result["symbol_results"]["A"]["trades"] *= 40
                await _store(session, execution_id="large", result=large_result)
                await session.commit()
                large = await _version(session)
                assert large - small == small - start
                assert 0 < large - small <= 4

    asyncio.run(check())


def test_late_insert_into_complete_graph_still_invalidates_projection(tmp_path):
    """完成后增加业务事实仍清除旧分析，不能绕过冻结后的失效边界。"""
    from axile.server.business_facts import trades
    from axile.server.business_facts.repository import _write_batch

    async def check():
        async with _database(tmp_path) as (_, sessions, _):
            async with sessions() as session:
                record = await _store(session)
                await _project(session, record)
                await session.commit()
                previous = await _version(session)
                assert record.facts_complete
                await _write_batch(
                    session, trades.encode_trades(record.id, {"trades": [{"trade_id": "late", "trade_volume": 0}]})
                )
                await session.commit()
                assert await _version(session) > previous
                assert await session.scalar(sa.select(sa.func.count()).select_from(source_execution)) == 0

    asyncio.run(check())
