"""类型化执行 API 的摘要边界与账户隔离。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.api.business_execution import (
    ExecuteRecordDetailPublic,
    ExecuteRecordSummaryPublic,
    read_execution_summaries,
)
from axile.server.api.routes import account_crud, account_execution, account_schedule
from axile.server.db.models.analysis_source import source_execution, source_execution_symbol, source_trade
from axile.server.db.models.execution import ExecuteRecord
from scripts.business_facts_contract import business_evidence
from tests.unit.server._journal_oracle import _compact_execution_activity


@asynccontextmanager
async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def test_execution_summaries_expose_only_head_and_compact_projection():
    """摘要保留显式空值和零，不读取完整业务事实。"""

    async def scenario():
        async with _session() as session:
            record = ExecuteRecord(account_id=1, is_success=1, status="NOOP", execution_time=0.0)
            session.add(record)
            await session.flush()
            await session.execute(
                source_execution.insert().values(
                    record_id=record.id,
                    account_id=1,
                    has_symbol_results=True,
                    trade_count=0,
                    symbol_count=0,
                )
            )
            await session.execute(
                source_execution_symbol.insert().values(
                    record_id=record.id,
                    symbol="SHFE.rb2610",
                    present=3,
                    status="NOOP",
                    error=None,
                )
            )
            summary = (await read_execution_summaries(session, [record]))[record.id]
            payload = summary.model_dump()
            assert payload["trade_count"] == payload["symbol_count"] == 0
            assert payload["execution_time"] == 0.0
            assert payload["symbol_results"] == {"SHFE.rb2610": {"status": "NOOP", "error": None}}
            assert not {"raw_input", "raw_result", "input", "result", "field_metadata"}.intersection(payload)

    asyncio.run(scenario())


def test_missing_projection_keeps_counts_unknown():
    """投影未发布时，摘要不把未知成交数量填为零。"""

    async def scenario():
        async with _session() as session:
            record = ExecuteRecord(account_id=1, is_success=0)
            session.add(record)
            await session.flush()
            summary = (await read_execution_summaries(session, [record]))[record.id]
            assert summary.trade_count is None
            assert summary.symbol_count is None
            assert summary.symbol_results is None

    asyncio.run(scenario())


@pytest.mark.parametrize("known", [False, True])
def test_activity_distinguishes_unknown_from_explicit_no_trades(known):
    """未知成本摘要保持空值，已投影的零成交仍是明确的零。"""
    record = ExecuteRecord(id=1, account_id=1, is_success=0)
    summary = ExecuteRecordSummaryPublic.model_validate(record)
    if known:
        summary.trade_count = 0
    activity = _compact_execution_activity(record, summary, [], None)
    assert activity.record.trade_count == (0 if known else None)
    if known:
        assert activity.record.summary is not None
        assert activity.record.summary.count == 0
    else:
        assert activity.record.summary is None


def test_detail_rejects_record_from_other_account_before_fact_loading(monkeypatch):
    """通过别的账户路径访问记录时，不重建或泄露任何明细。"""

    async def account_exists(_session, account_id):
        return SimpleNamespace(id=account_id)

    monkeypatch.setattr(account_crud, "_get_account_or_404", account_exists)

    async def scenario():
        async with _session() as session:
            record = ExecuteRecord(account_id=2, is_success=1)
            session.add(record)
            await session.flush()
            with pytest.raises(HTTPException) as error:
                await account_crud.execute_record_detail(session, account_id=1, record_id=record.id)
            assert error.value.status_code == 404

    asyncio.run(scenario())


def test_detail_reads_canonical_facts_without_legacy_documents(monkeypatch):
    """单条详情使用规范仓库，稳定字段与冻结输入保留原语义。"""
    from axile.server.business_facts.repository import store_execution_facts

    async def account_exists(_session, account_id):
        return SimpleNamespace(id=account_id)

    monkeypatch.setattr(account_crud, "_get_account_or_404", account_exists)

    async def scenario():
        async with _session() as session:
            record = ExecuteRecord(account_id=1, execution_id="canonical-detail", is_success=1)
            session.add(record)
            await session.flush()
            source_input = {"channel_type": "ctp", "curr_target": {}, "last_target": None}
            source_result = {"status": "NOOP", "execution_time": 0.0, "symbol_results": {}}
            await store_execution_facts(session, record, source_input, source_result)
            payload = await account_crud.execute_record_detail(session, account_id=1, record_id=record.id)
            assert payload.input == source_input
            assert business_evidence(payload.result) == business_evidence(source_result)
            assert payload.result["task_status"] == "SUCCEEDED"
            assert payload.status == "NOOP"
            assert payload.execution_time == 0.0
            assert not {"raw_input", "raw_result"}.intersection(payload.model_dump())

    asyncio.run(scenario())


def test_activity_symbol_query_uses_projected_columns_and_account_scope(monkeypatch):
    """成本行不依赖完整结果载荷，同时保留零费用及成交身份。"""

    async def account_exists(_session, account_id):
        return SimpleNamespace(id=account_id)

    monkeypatch.setattr(account_schedule, "_get_account_or_404", account_exists)

    async def scenario():
        async with _session() as session:
            for account_id in (1, 2):
                record = ExecuteRecord(
                    account_id=account_id,
                    execution_id=f"execution-{account_id}",
                    created_at="2026-10-20T09:00:00",
                    is_success=1,
                )
                session.add(record)
                await session.flush()
                await session.execute(source_execution.insert().values(record_id=record.id, account_id=account_id))
                await session.execute(
                    source_trade.insert().values(
                        record_id=record.id,
                        trade_index=0,
                        symbol="SHFE.rb2610",
                        time=1234.0,
                        day="2026-10-20",
                        value=1000.0,
                        cost=0.0,
                        loss_bp=0.0,
                        fee=0.0,
                        fee_currency="CNY",
                        estimated=False,
                        side="buy",
                        quantity=1.0,
                        price=100.0,
                        reference=100.0,
                        reference_source="mid",
                    )
                )
            payload = await account_schedule.account_activity_symbols(
                session,
                account_id=1,
                since="2026-10-20T00:00:00",
                until="2026-10-21T00:00:00",
                symbol="SHFE.rb2610",
            )
            assert payload.count == 1
            symbol = payload.data[0]
            assert symbol.n_trades == 1
            assert symbol.summary.value == 1000.0
            assert symbol.summary.fees == {"CNY": 0.0}
            assert symbol.trades[0]["execution_id"] == "execution-1"
            assert symbol.trades[0]["timeEstimated"] is False

    asyncio.run(scenario())


def test_target_sizing_uses_immutable_audit_input_context():
    """历史审计输入与终态输入不同，换算精度必须跟随审计引用。"""
    from axile.domain.execution import ExecutionArtifactType
    from axile.server.business_facts.artifacts import make_artifact_reference
    from axile.server.business_facts.repository import (
        persist_execution_input,
        read_execution_input,
        store_execution_facts,
    )
    from axile.server.db.models.execution import ExecutionArtifact
    from axile.server.target_weight_snapshots import TargetWeightSnapshotData

    async def scenario():
        async with _session() as session:
            record = ExecuteRecord(account_id=1, execution_id="sizing-context", is_success=1)
            session.add(record)
            await session.flush()
            terminal_input = {"channel_type": "ctp", "curr_target": {"rb2610": 1}}
            await store_execution_facts(
                session,
                record,
                terminal_input,
                {"symbol_results": {"rb2610": {"sizing": {"reason_code": "SIZED", "target_quantity": 2}}}},
            )
            audit_input_id = await persist_execution_input(
                session,
                account_id=1,
                execution_id=None,
                owner_execution_id=record.execution_id,
                created_at=record.created_at,
                payload={"sizing_context": {"weight_precision": 0.01}},
            )
            session.add(
                ExecutionArtifact(
                    execution_id=record.execution_id,
                    artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
                    schema_version=2,
                    content=make_artifact_reference(view="targets", input_id=audit_input_id),
                )
            )
            await session.flush()
            snapshot = TargetWeightSnapshotData(
                id=1,
                portfolio_id=1,
                account_id=1,
                raw_weights={"rb2610": 1},
                normalized_weights={"rb2610": 1},
                source="execution",
                execution_id=record.execution_id,
                calculated_at=record.created_at,
            )
            evidence = await account_execution._sizing_evidence_for_snapshot(
                session,
                snapshot,
                canonicalize=lambda symbol: symbol,
                strategy_weights={"rb2610": 1},
                account_weights={"rb2610": 1},
            )
            assert evidence.status == "available"
            assert evidence.rows["rb2610"].weight_precision == 0.01
            assert await read_execution_input(session, record.input_id) == terminal_input

    asyncio.run(scenario())


@pytest.mark.parametrize("delete_account", [False, True])
def test_delete_execution_records_preserves_independent_summary_journal(monkeypatch, delete_account):
    """真实引用经应用删除后仍可读，且结果事实级联删除不影响其他账户。"""
    from copy import deepcopy

    from sqlalchemy import func, select, text

    from axile.domain.execution import ExecutionArtifactType
    from axile.server.business_facts.artifacts import compact_execution_summary, resolve_artifact_content
    from axile.server.business_facts.extensions import execution_extension
    from axile.server.business_facts.repository import store_execution_facts
    from axile.server.db.models.execution import ExecutionArtifact
    from tests.unit.server._execution_test_support import build_account

    async def drop_worker(_account_id):
        return None

    monkeypatch.setattr(account_crud, "delete_job", lambda _sched, _account_id: None)
    monkeypatch.setattr(account_crud, "drop_account_worker", drop_worker)

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("PRAGMA foreign_keys=ON"))
                await connection.run_sync(SQLModel.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add_all([build_account(id=1, portfolio_id=None), build_account(id=2, portfolio_id=None)])
                await session.flush()
                record = ExecuteRecord(account_id=1, execution_id="delete-summary", is_success=1)
                other = ExecuteRecord(account_id=2, execution_id="keep-other", is_success=1)
                session.add_all([record, other])
                await session.flush()
                result = {
                    "status": "SUCCEEDED",
                    "symbol_results": {
                        "rb2610": {
                            "status": "SUCCEEDED",
                            "sizing": {"reason_code": "SIZED", "target_quantity": 1},
                            "orders": [{"order_id": "o1", "volume": 1, "filled_volume": 1}],
                            "trades": [{"trade_id": "t1", "trade_volume": 1, "trade_price": 100}],
                        }
                    },
                }
                await store_execution_facts(session, record, {"curr_target": {"rb2610": 1}}, result)
                journal = {**deepcopy(result), "journal_note": {"independent": True}}
                artifact = ExecutionArtifact(
                    execution_id=record.execution_id,
                    artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
                    schema_version=2,
                    content=deepcopy(journal),
                )
                session.add(artifact)
                await session.flush()
                await compact_execution_summary(session, record, result=result)
                await session.refresh(artifact)
                assert "business_facts_ref" in artifact.content
                artifact_id = artifact.id
                await session.commit()

                if delete_account:
                    message = await account_crud.delete_account(session, sched=SimpleNamespace(), account_id=1)
                    assert message.message == "成功删除账户"
                else:
                    message = await account_crud.delete_execute_records(session, account_id=1)
                    assert message.message == "成功删除"
                assert await session.get(ExecuteRecord, record.id, populate_existing=True) is None
                assert await session.get(ExecuteRecord, other.id, populate_existing=True) is not None
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(execution_extension)
                        .where(execution_extension.c.record_id == record.id)
                    )
                    == 0
                )
                artifact = await session.get(ExecutionArtifact, artifact_id, populate_existing=True)
                assert artifact is not None
                assert artifact.schema_version == 2
                assert "business_facts_ref" not in artifact.content
                assert business_evidence(
                    await resolve_artifact_content(session, artifact, account_id=1)
                ) == business_evidence(journal)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("input_value,result_value", [(None, []), (False, "invalid"), ({}, {})])
def test_detail_keeps_historical_root_shapes(input_value, result_value):
    """畸形历史根值保持原始证据，不被模型改写为空对象。"""
    detail = ExecuteRecordDetailPublic.model_validate(
        {
            "id": 1,
            "account_id": 1,
            "execution_id": None,
            "created_at": "2026-10-20T09:00:00",
            "is_success": 0,
            "input": input_value,
            "result": result_value,
        }
    )
    assert detail.input == input_value
    assert detail.result == result_value
