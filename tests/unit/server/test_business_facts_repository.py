"""业务事实事务保留下单前输入及统一资产观测引用。"""

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.business_facts import assets, inputs, repository
from axile.server.db.models import ExecuteRecord
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account


def test_domains_cannot_replace_malformed_symbol_container():
    destination = {"symbol_results": {"A": None}}
    with pytest.raises(ValueError, match="字段冲突"):
        repository.merge_result_fragment(destination, {"symbol_results": {"A": {"orders": []}}})


def test_nonempty_result_root_array_is_rejected_before_writing():
    with pytest.raises(ValueError, match="Unsupported historical result root"):
        repository.encode_result_facts(1, [{"orders": []}])


@pytest.mark.parametrize("foreign_after_asset", [False, True])
@pytest.mark.parametrize("attached_input", [False, True])
@pytest.mark.parametrize("result_inputs", [False, True])
def test_final_result_reuses_durable_normalized_input_and_after_asset(
    tmp_path, foreign_after_asset, attached_input, result_inputs
):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        frozen = {"curr_target": {"normalized": 0}, "channel_type": "tq"}
        observed = {"total_asset": 0.0, "positions": [], "extra": {}}
        output = {"status": "NOOP", "account_assets": observed, "symbol_results": {}}
        if result_inputs:
            output["inputs"] = frozen
        input_reads = []

        def track_input_reads(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                input_reads.extend(table.name for table in inputs.TABLES if f"FROM {table.name} \n" in statement)

        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with sessions() as session:
                session.add(build_account(id=2, account_control_preset="default"))
                await session.commit()
                input_id = await repository.persist_execution_input(
                    session, account_id=2, execution_id="frozen", payload=frozen, created_at="2026-10-20T09:00:00"
                )
                await session.commit()
                after_ref = await repository.persist_execution_after_assets(
                    session, account_id=2, execution_id="frozen", payload=output
                )
                await session.commit()
                record = ExecuteRecord(
                    account_id=2, execution_id="frozen", is_success=1, input_id=input_id if attached_input else None
                )
                session.add(record)
                await session.flush()
                sa.event.listen(engine.sync_engine, "before_cursor_execute", track_input_reads)
                canonical_input = await repository.store_execution_facts(
                    session, record, raw_input={"curr_target": {"stale": 1}}, raw_result=output
                )
                sa.event.remove(engine.sync_engine, "before_cursor_execute", track_input_reads)
                assert business_evidence(canonical_input) == business_evidence(frozen)
                assert input_reads == [table.name for table in inputs.TABLES]
                await session.commit()
                detail = await repository.read_execution_detail(session, record)
                assert business_evidence(detail["input"]) == business_evidence(frozen)
                assert business_evidence(detail["result"]) == business_evidence(output)
                async with engine.connect() as connection:
                    assert business_evidence(
                        await connection.run_sync(repository.read_execution_result_sync, record.id)
                    ) == business_evidence(output)
                    assert business_evidence(
                        await connection.run_sync(repository.read_execution_input_sync, input_id)
                    ) == business_evidence(frozen)
                assert record.input_id == input_id
                assert record.result_input_id == (input_id if result_inputs else None)
                assert business_evidence(after_ref) == business_evidence(
                    (input_id, assets.input_after_asset_fact_id(input_id))
                )
                assert business_evidence(
                    await session.scalar(sa.select(sa.func.count()).select_from(inputs.execution_input))
                ) == business_evidence(1)
                assert business_evidence(
                    await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_snapshot))
                ) == business_evidence(1)
                assert business_evidence(
                    await session.scalar(sa.select(assets.asset_binding.c.asset_id))
                ) == business_evidence(after_ref[1])
                sources = dict(repository.execution_fact_sources(record_ids=(record.id,)))
                assert assets.asset_position in sources
                assert assets.asset_snapshot in sources
                if foreign_after_asset:
                    other_input = await repository.persist_execution_input(
                        session, account_id=2, execution_id="other", payload={}, created_at=record.created_at
                    )
                    await repository.persist_execution_after_assets(
                        session, account_id=2, execution_id="other", payload={"account_assets": {"total_asset": 99}}
                    )
                    await session.execute(
                        assets.asset_binding.update()
                        .where(assets.asset_binding.c.record_id == record.id)
                        .values(asset_id=assets.input_after_asset_fact_id(other_input))
                    )
                    await session.commit()
                    with pytest.raises(ValueError):
                        await repository.read_execution_result(session, record)
                    async with engine.connect() as connection:
                        with pytest.raises(ValueError):
                            await connection.run_sync(repository.read_execution_result_sync, record.id)
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("is_success", [0, 1])
@pytest.mark.parametrize("result_assets", ["absent", "matching", "different"])
def test_input_reuse_preserves_failure_finalization_after_assets(tmp_path, is_success, result_assets):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'after-assets.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        frozen = {"curr_target": {"A": 0}, "last_target": None}
        observed = {"total_asset": 0, "positions": []}
        output = {"status": "SUCCEEDED" if is_success else "FAILED"}
        if result_assets != "absent":
            output["account_assets"] = observed if result_assets == "matching" else {"total_asset": 1}
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with sessions() as session:
                session.add(build_account(id=2, account_control_preset="default"))
                await session.commit()
                await repository.persist_execution_input(
                    session, account_id=2, execution_id="after-assets", payload=frozen, created_at="2026-10-20"
                )
                await repository.persist_execution_after_assets(
                    session, account_id=2, execution_id="after-assets", payload={"account_assets": observed}
                )
                await session.commit()
                record = ExecuteRecord(account_id=2, execution_id="after-assets", is_success=is_success)
                session.add(record)
                await session.flush()
                if result_assets == "different" or (is_success and result_assets == "absent"):
                    with pytest.raises(ValueError, match="终态结果"):
                        await repository.store_execution_facts(session, record, raw_input=None, raw_result=output)
                    await session.rollback()
                    assert await session.scalar(sa.select(sa.func.count()).select_from(ExecuteRecord)) == 0
                    return
                canonical_input = await repository.store_execution_facts(
                    session, record, raw_input=None, raw_result=output
                )
                assert canonical_input == frozen
                await session.commit()
                result = await repository.read_execution_result(session, record)
                assert business_evidence(result) == business_evidence(output)
                assert await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_snapshot)) == 1
                assert await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_binding)) == (
                    1 if result_assets == "matching" else 0
                )
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("reference", ["input_id", "result_input_id"])
@pytest.mark.parametrize("input_account", [2, 3])
def test_foreign_input_is_rejected_on_read_and_write(tmp_path, reference, input_account):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'owners.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with sessions() as session:
                session.add(build_account(id=2, account_control_preset="default"))
                await session.commit()
                session.add(build_account(id=3, account_control_preset="default"))
                await session.commit()
                unrelated = await repository.persist_execution_input(
                    session,
                    account_id=input_account,
                    execution_id="other",
                    payload={},
                    created_at="2026-10-20T09:00:00",
                )
                record = ExecuteRecord(account_id=2, execution_id="own", is_success=1)
                session.add(record)
                await session.flush()
                await repository.store_execution_facts(session, record, raw_input={}, raw_result={"inputs": {}})
                await session.commit()
                await session.execute(
                    ExecuteRecord.__table__.update()
                    .where(ExecuteRecord.__table__.c.id == record.id)
                    .values(**{reference: unrelated})
                )
                await session.commit()
                await session.refresh(record)
                message = "其他执行" if input_account == 2 else "其他账户"
                with pytest.raises(ValueError, match=message):
                    await repository.read_execution_detail(session, record)
                async with engine.connect() as connection:
                    with pytest.raises(ValueError, match=message):
                        await connection.run_sync(repository.read_execution_result_sync, record.id)
                with pytest.raises(ValueError, match=message):
                    await repository.store_execution_facts(session, record, raw_input=None, raw_result={})
                await session.rollback()
                await session.execute(
                    inputs.execution_input.update()
                    .where(inputs.execution_input.c.id == unrelated)
                    .values(owner_execution_id="another-execution")
                )
                with pytest.raises(ValueError, match=message):
                    await repository.find_execution_input(session, account_id=2, execution_id="other")
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_null_execution_ids_cannot_share_one_legacy_input(tmp_path):
    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'null-owners.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with sessions() as session:
                session.add(build_account(id=2, account_control_preset="default"))
                await session.commit()
                input_id = await repository.persist_execution_input(
                    session, account_id=2, execution_id=None, payload={}, created_at="2026-10-20T09:00:00"
                )
                first = ExecuteRecord(account_id=2, execution_id=None, is_success=1, input_id=input_id)
                session.add(first)
                await session.flush()
                await repository.store_execution_facts(session, first, raw_input=None, raw_result={})
                await session.commit()
                second = ExecuteRecord(account_id=2, execution_id=None, is_success=1, input_id=input_id)
                session.add(second)
                await session.flush()
                with pytest.raises(ValueError, match="历史输入不能属于其他执行记录"):
                    await repository.store_execution_facts(session, second, raw_input=None, raw_result={})
        finally:
            await engine.dispose()

    asyncio.run(check())
