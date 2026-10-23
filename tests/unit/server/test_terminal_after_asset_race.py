"""worker 后观测提交后，丢失成功回包不应阻断失败终态。"""

import asyncio
import json
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy

import pytest

from axile.domain.execution import ExecutionArtifactType, ExecutionKind, ExecutionTerminateMode
from axile.server.business_facts import repository
from axile.server.business_facts.artifacts import make_artifact_reference
from axile.server.db.models import ExecuteRecord, ExecutionArtifact
from axile.server.execution import input_persistence, records
from scripts.business_facts_contract import business_evidence
from tests.conftest import initialize_native_database, native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._execution_test_support import build_account, insert_account

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


def _rows(path, table):
    with closing(oracle_connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


@contextmanager
def _frozen_after(tmp_path, monkeypatch):
    path = tmp_path / "terminal-race.db"
    initialize_native_database(path)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    with closing(oracle_connect(path)) as connection, connection:
        insert_account(connection, build_account(id=_ACCOUNT_ID, portfolio_id=None))
    input_id = native_store_call(
        path,
        "freeze_execution_input",
        {
            "account_id": _ACCOUNT_ID,
            "execution_id": _EXECUTION_ID,
            "input_id": "race-input",
            "payload": _FROZEN_INPUT,
            "created_at": _CREATED_AT,
            "before_assets": _BEFORE_ASSETS,
        },
    )["input_id"]
    reference = input_persistence.persist_post_execute_assets_sync(
        account_id=_ACCOUNT_ID, execution_id=_EXECUTION_ID, payload=_WORKER_RESULT
    )
    assert reference is not None
    asset_id = reference[1]
    artifact = ExecutionArtifact(
        execution_id=_EXECUTION_ID,
        artifact_type=ExecutionArtifactType.ACCOUNT_SNAPSHOT,
        schema_version=3,
        content=make_artifact_reference(view="assets_after", input_id=input_id, asset_id=asset_id),
    )
    assert native_store_call(path, "append_execution_artifact", artifact.model_dump(mode="json"))
    yield path, input_id, asset_id


def _asset_evidence(path):
    return {table: _rows(path, table) for table in ("business_asset_snapshot", "business_asset_position")}


def _resolved_artifact(path):
    artifacts = native_store_call(path, "list_execution_artifacts", {"execution_id": _EXECUTION_ID})
    assert artifacts["count"] == 1
    return artifacts["data"][0]["content"]


async def _append_terminal(terminal, raw_result=None):
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
        )
    if terminal == "exception":
        return await records.append_error_execute_record(
            account_id=_ACCOUNT_ID,
            execution_id=_EXECUTION_ID,
            msg="synthetic exception before worker response",
            raw_result=raw_result,
        )
    assert terminal == "success"
    return await records.append_success_execute_record(
        account=build_account(id=_ACCOUNT_ID),
        execution_id=_EXECUTION_ID,
        raw_input={},
        result=raw_result,
    )


@pytest.mark.parametrize("terminal", ["terminated", "exception"])
def test_missing_worker_response_persists_failure_without_inventing_result_assets(tmp_path, monkeypatch, terminal):
    async def check():
        with _frozen_after(tmp_path, monkeypatch) as (path, input_id, asset_id):
            frozen_evidence = _asset_evidence(path)
            record = await _append_terminal(terminal)
            stored_row = _rows(path, "executerecord")[0]
            for field in ("audit_evidence", "termination_evidence"):
                stored_row[field] = json.loads(stored_row[field])
            stored = ExecuteRecord.model_validate(stored_row)
            assert stored.facts_complete
            assert stored.is_success == 0
            assert stored.task_status == ("TERMINATED" if terminal == "terminated" else "FAILED")
            assert stored.input_id == input_id
            result = await repository.read_execution_result(stored)
            assert "account_assets" not in result
            assert "success" not in result
            if terminal == "terminated":
                assert result["outcome"] == "terminated"
                assert result["termination"]["reason"] == "synthetic termination before worker response"
            else:
                assert result["status"] == "FAILED"
                assert result["error"] == "synthetic exception before worker response"
            assert _asset_evidence(path) == frozen_evidence
            assert _rows(path, "business_asset_binding") == []
            assert _rows(path, "account_asset_snapshot") == []
            artifact = _rows(path, "execution_artifact")[0]
            assert json.loads(artifact["content"]) == make_artifact_reference(
                view="assets_after", input_id=input_id, asset_id=asset_id
            )
            assert business_evidence(_resolved_artifact(path)["account_assets"]) == business_evidence(_AFTER_ASSETS)
            late_success = await _append_terminal("success", _WORKER_RESULT)
            assert late_success.id == record.id
            assert late_success.is_success == 0
            assert late_success.task_status == stored.task_status
            result = await repository.read_execution_result(late_success)
            assert "account_assets" not in result
            assert _asset_evidence(path) == frozen_evidence

    asyncio.run(check())


@pytest.mark.parametrize("terminal", ["success", "terminated", "exception"])
def test_matching_after_observation_reuses_immutable_fact(tmp_path, monkeypatch, terminal):
    async def check():
        with _frozen_after(tmp_path, monkeypatch) as (path, input_id, asset_id):
            frozen_evidence = _asset_evidence(path)
            payload = {"account_assets": deepcopy(_AFTER_ASSETS)}
            record = await _append_terminal(terminal, payload)
            assert payload == {"account_assets": _AFTER_ASSETS}
            assert record.facts_complete
            assert record.is_success == int(terminal == "success")
            assert record.input_id == input_id
            result = await repository.read_execution_result(record)
            assert business_evidence(result["account_assets"]) == business_evidence(_AFTER_ASSETS)
            assert _asset_evidence(path) == frozen_evidence
            assert [row["asset_id"] for row in _rows(path, "business_asset_binding")] == [asset_id]
            history = _rows(path, "account_asset_snapshot")
            assert len(history) == 1 and history[0]["asset_fact_id"] == asset_id

    asyncio.run(check())


@pytest.mark.parametrize("terminal", ["success", "terminated", "exception"])
@pytest.mark.parametrize("change", ["equity", "time", "position", "null"])
def test_conflicting_after_observation_rolls_back_terminal_without_overwriting_evidence(
    tmp_path, monkeypatch, terminal, change
):
    async def check():
        with _frozen_after(tmp_path, monkeypatch) as (path, _input_id, _asset_id):
            frozen_evidence = _asset_evidence(path)
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
                await _append_terminal(terminal, {"account_assets": changed})
            assert _rows(path, "executerecord") == []
            assert _rows(path, "business_asset_binding") == []
            assert _rows(path, "account_asset_snapshot") == []
            assert _asset_evidence(path) == frozen_evidence
            assert business_evidence(_resolved_artifact(path)["account_assets"]) == business_evidence(_AFTER_ASSETS)

    asyncio.run(check())


def test_success_missing_frozen_after_observation_is_still_rejected(tmp_path, monkeypatch):
    async def check():
        with _frozen_after(tmp_path, monkeypatch) as (path, _input_id, _asset_id):
            frozen_evidence = _asset_evidence(path)
            with pytest.raises(ValueError, match="终态结果缺少 account_assets"):
                await _append_terminal("success", {"status": "SUCCEEDED", "success": True})
            assert _rows(path, "executerecord") == []
            assert _asset_evidence(path) == frozen_evidence

    asyncio.run(check())
