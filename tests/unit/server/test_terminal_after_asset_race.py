"""worker 后观测提交后，丢失成功回包不应阻断失败终态。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.domain.execution import ExecutionArtifactType, ExecutionKind, ExecutionTerminateMode
from axile.server.business_facts import assets, repository
from axile.server.business_facts.artifacts import make_artifact_reference, resolve_artifact_content
from axile.server.db.models import AccountAssetSnapshot, ExecuteRecord, ExecutionArtifact
from axile.server.execution import input_persistence, records
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account

_ACCOUNT_ID = 2
_EXECUTION_ID = "after-assets-before-response"
_CREATED_AT = "2026-10-20T09:00:00"
_FROZEN_INPUT = {"curr_target": {"A": 2}}
_BEFORE_ASSETS = {"total_asset": 100.0, "positions": []}
_AFTER_ASSETS = {
    "available_cash": 20.0,
    "total_asset": 100.0,
    "market_value": 80.0,
    "source": "synthetic",
    "update_time": _CREATED_AT,
    "positions": [{"symbol": "A", "volume": 2.0, "market_value": 80.0}],
}
_WORKER_RESULT = {"status": "SUCCEEDED", "success": True, "account_assets": _AFTER_ASSETS}


@asynccontextmanager
async def _frozen_after(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'terminal-race.db'}")
    # 执行后观测经原生入口冻结,须指向同一测试文件。
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(tmp_path / "terminal-race.db"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with sessions() as session:
            session.add(build_account(id=_ACCOUNT_ID))
            input_id = await repository.persist_execution_input(
                session,
                account_id=_ACCOUNT_ID,
                execution_id=_EXECUTION_ID,
                payload=_FROZEN_INPUT,
                created_at=_CREATED_AT,
                before_assets=_BEFORE_ASSETS,
            )
            await session.commit()
        async with sessions() as session:
            reference = await input_persistence._persist_post_execute_assets(
                account_id=_ACCOUNT_ID, execution_id=_EXECUTION_ID, payload=_WORKER_RESULT
            )
            assert reference is not None
            asset_id = reference[1]
            session.add(
                ExecutionArtifact(
                    execution_id=_EXECUTION_ID,
                    artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT,
                    schema_version=3,
                    content=make_artifact_reference(view="assets_after", input_id=input_id, asset_id=asset_id),
                )
            )
            await session.commit()
        yield sessions, input_id, asset_id
    finally:
        await engine.dispose()


async def _asset_evidence(session):
    return {
        table.name: [dict(row) for row in (await session.execute(sa.select(table))).mappings()]
        for table in (assets.asset_snapshot, assets.asset_position)
    }


async def _append_terminal(sessions, terminal, raw_result=None):
    if terminal == "terminated":
        return await records.append_terminated_execute_record(
            account_id=_ACCOUNT_ID,
            execution_id=_EXECUTION_ID,
            execution_kind=ExecutionKind.REBALANCE,
            reason="synthetic termination before worker response",
            mode=ExecutionTerminateMode.GRACEFUL,
            requested_at=_CREATED_AT,
            acked_at=None,
            finished_at="2026-10-20T09:00:01",
            cancel_attempted=False,
            cancel_failed_order_ids=[],
            raw_result=raw_result,
            session_factory=sessions,
        )
    if terminal == "exception":
        return await records.append_error_execute_record(
            account_id=_ACCOUNT_ID,
            execution_id=_EXECUTION_ID,
            msg="synthetic exception before worker response",
            raw_result=raw_result,
            session_factory=sessions,
        )
    assert terminal == "success"
    return await records.append_success_execute_record(
        account=build_account(id=_ACCOUNT_ID),
        execution_id=_EXECUTION_ID,
        raw_input={},
        result=raw_result,
        session_factory=sessions,
    )


@pytest.mark.parametrize("terminal", ["terminated", "exception"])
def test_missing_worker_response_persists_failure_without_inventing_result_assets(tmp_path, monkeypatch, terminal):
    async def check():
        async with _frozen_after(tmp_path, monkeypatch) as (sessions, input_id, asset_id):
            async with sessions() as session:
                frozen_evidence = await _asset_evidence(session)
            record = await _append_terminal(sessions, terminal)
            async with sessions() as session:
                stored = await session.get(ExecuteRecord, record.id)
                assert stored is not None and stored.facts_complete
                assert stored.is_success == 0
                assert stored.task_status == ("TERMINATED" if terminal == "terminated" else "FAILED")
                assert stored.input_id == input_id
                result = await repository.read_execution_result(session, stored)
                assert "account_assets" not in result
                assert "success" not in result
                if terminal == "terminated":
                    assert result["outcome"] == "terminated"
                    assert result["termination"]["reason"] == "synthetic termination before worker response"
                else:
                    assert result["status"] == "FAILED"
                    assert result["error"] == "synthetic exception before worker response"
                assert await _asset_evidence(session) == frozen_evidence
                assert await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_binding)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(AccountAssetSnapshot)) == 0
                artifact = (await session.scalars(sa.select(ExecutionArtifact))).one()
                assert artifact.content == make_artifact_reference(
                    view="assets_after", input_id=input_id, asset_id=asset_id
                )
                resolved = await resolve_artifact_content(session, artifact, account_id=_ACCOUNT_ID)
                assert business_evidence(resolved["account_assets"]) == business_evidence(_AFTER_ASSETS)
            late_success = await _append_terminal(sessions, "success", _WORKER_RESULT)
            assert late_success.id == record.id
            assert late_success.is_success == 0
            assert late_success.task_status == stored.task_status
            async with sessions() as session:
                result = await repository.read_execution_result(session, late_success)
                assert "account_assets" not in result
                assert await _asset_evidence(session) == frozen_evidence

    asyncio.run(check())


@pytest.mark.parametrize("terminal", ["success", "terminated", "exception"])
def test_matching_after_observation_reuses_immutable_fact(tmp_path, monkeypatch, terminal):
    async def check():
        async with _frozen_after(tmp_path, monkeypatch) as (sessions, input_id, asset_id):
            async with sessions() as session:
                frozen_evidence = await _asset_evidence(session)
            payload = {"account_assets": deepcopy(_AFTER_ASSETS)}
            record = await _append_terminal(sessions, terminal, payload)
            assert payload == {"account_assets": _AFTER_ASSETS}
            async with sessions() as session:
                assert record.facts_complete
                assert record.is_success == int(terminal == "success")
                assert record.input_id == input_id
                result = await repository.read_execution_result(session, record)
                assert business_evidence(result["account_assets"]) == business_evidence(_AFTER_ASSETS)
                assert await _asset_evidence(session) == frozen_evidence
                assert (await session.scalars(sa.select(assets.asset_binding.c.asset_id))).one() == asset_id
                history = (await session.scalars(sa.select(AccountAssetSnapshot))).one()
                assert history.asset_fact_id == asset_id

    asyncio.run(check())


@pytest.mark.parametrize("terminal", ["success", "terminated", "exception"])
@pytest.mark.parametrize("change", ["equity", "time", "position", "null"])
def test_conflicting_after_observation_rolls_back_terminal_without_overwriting_evidence(
    tmp_path, monkeypatch, terminal, change
):
    async def check():
        async with _frozen_after(tmp_path, monkeypatch) as (sessions, _input_id, _asset_id):
            async with sessions() as session:
                frozen_evidence = await _asset_evidence(session)
            changed = deepcopy(_AFTER_ASSETS)
            if change == "equity":
                changed["total_asset"] = 101.0
            elif change == "time":
                changed["update_time"] = "2026-10-20T09:00:01"
            elif change == "position":
                changed["positions"][0]["volume"] = 3.0
            else:
                changed = None
            with pytest.raises(ValueError, match="与已持久化执行后资产观测不一致"):
                await _append_terminal(sessions, terminal, {"account_assets": changed})
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(ExecuteRecord)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(assets.asset_binding)) == 0
                assert await session.scalar(sa.select(sa.func.count()).select_from(AccountAssetSnapshot)) == 0
                assert await _asset_evidence(session) == frozen_evidence
                artifact = (await session.scalars(sa.select(ExecutionArtifact))).one()
                resolved = await resolve_artifact_content(session, artifact, account_id=_ACCOUNT_ID)
                assert business_evidence(resolved["account_assets"]) == business_evidence(_AFTER_ASSETS)

    asyncio.run(check())


def test_success_missing_frozen_after_observation_is_still_rejected(tmp_path, monkeypatch):
    async def check():
        async with _frozen_after(tmp_path, monkeypatch) as (sessions, _input_id, _asset_id):
            async with sessions() as session:
                frozen_evidence = await _asset_evidence(session)
            with pytest.raises(ValueError, match="终态结果缺少 account_assets"):
                await _append_terminal(sessions, "success", {"status": "SUCCEEDED", "success": True})
            async with sessions() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(ExecuteRecord)) == 0
                assert await _asset_evidence(session) == frozen_evidence

    asyncio.run(check())
