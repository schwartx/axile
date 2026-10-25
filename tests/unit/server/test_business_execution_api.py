"""类型化执行 API 的摘要边界与账户隔离。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from axile.domain.execution import ExecutionArtifactType
from axile.server._native import _core
from axile.server.api.routes import account_crud, account_execution, account_schedule
from axile.server.business_facts.artifacts import make_artifact_reference, resolve_artifact_content
from axile.server.business_facts.repository import read_execution_input
from axile.server.core import db
from axile.server.db.models.execution import ExecuteRecord
from axile.server.target_weight_snapshots import TargetWeightSnapshotData
from scripts.business_facts_contract import business_evidence
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import freeze_input, persist_result, rows, sql
from tests.unit.server._execution_read_oracle import ExecuteRecordSummaryPublic
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._journal_oracle import _compact_execution_activity


@pytest.fixture
def database(native_database, monkeypatch):
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(native_database)))
    monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(native_database)))
    for account_id in (1, 2):
        receipt = native_store_call(
            native_database,
            "account_create",
            {"now": "2026-10-20", "account": build_account(id=account_id, portfolio_id=None).model_dump(mode="json")},
        )
        assert receipt["account"]["id"] == account_id
    return native_database


def _summary_page():
    return asyncio.run(account_crud.list_execute_records(1, SimpleNamespace(skip=0, limit=100)))


def _append_artifact(database, execution_id, artifact_type, content):
    assert native_store_call(
        database,
        "append_execution_artifact",
        {
            "execution_id": execution_id,
            "artifact_type": artifact_type.value,
            "schema_version": 2,
            "content": content,
            "created_at": "2026-10-20",
        },
    )


def test_execution_summaries_expose_only_head_and_compact_projection(database):
    """摘要保留显式空值和零，不读取完整业务事实。"""
    persist_result(
        database,
        {
            "status": "NOOP",
            "execution_time": 0.0,
            "symbol_results": {"SHFE.rb2610": {"status": "NOOP", "error": None}},
        },
        raw_input={},
        is_success=1,
    )
    payload = json.loads(_summary_page().body)["data"][0]
    assert payload["trade_count"] == payload["symbol_count"] == 0
    assert payload["execution_time"] == 0.0
    assert payload["symbol_results"] == {"SHFE.rb2610": {"status": "NOOP", "outcome": "completed"}}
    assert not {"raw_input", "raw_result", "input", "result", "field_metadata"}.intersection(payload)


def test_missing_projection_keeps_counts_unknown(database):
    """投影未发布时，摘要不把未知成交数量填为零。"""
    record = persist_result(database, {})
    sql(database, "UPDATE executerecord SET facts_complete=0 WHERE id=?", (record["id"],))
    with pytest.raises(HTTPException) as caught:
        _summary_page()
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "SOURCE_FACTS_INCOMPLETE"


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
        assert activity.record.summary["count"] == 0
    else:
        assert activity.record.summary is None


def test_activity_symbol_query_uses_projected_columns_and_account_scope(database):
    """成本行不依赖完整结果载荷，同时保留零费用及成交身份。"""
    for account_id in (1, 2):
        persist_result(
            database,
            {
                "symbol_results": {
                    "SHFE.rb2610": {
                        "first_tick": {"bid_price": 100, "ask_price": 100},
                        "sizing": {"unit_multiplier": 10},
                        "trades": [
                            {
                                "trade_time": "2026-10-20T09:00:00",
                                "trade_price": 100,
                                "trade_volume": 1,
                                "fee_currency": "CNY",
                                "extra": {"direction": "buy", "commission": 0, "commission_asset": "CNY"},
                            }
                        ],
                    }
                }
            },
            account_id=account_id,
            execution_id=f"execution-{account_id}",
            created_at="2026-10-20T09:00:00",
            is_success=1,
            raw_input={},
        )
    payload = json.loads(
        asyncio.run(
            account_schedule.account_activity_symbols(
                account_id=1,
                since="2026-10-20T00:00:00",
                until="2026-10-21T00:00:00",
                symbol="SHFE.rb2610",
            )
        ).body
    )
    assert payload["count"] == 1
    symbol = payload["data"][0]
    assert symbol["n_trades"] == 1
    assert symbol["summary"]["value"] == 1000.0
    assert symbol["summary"]["fees"] == {"CNY": 0.0}
    assert symbol["trades"][0]["execution_id"] == "execution-1"
    assert symbol["trades"][0]["timeEstimated"] is False


def test_target_sizing_uses_immutable_audit_input_context(database):
    """历史审计输入与终态输入不同，换算精度必须跟随审计引用。"""
    terminal_input = {"channel_type": "ctp", "curr_target": {"rb2610": 1}}
    frozen = freeze_input(
        database,
        terminal_input,
        execution_id="sizing-context",
        audit_input_id="sizing-audit",
        audit_payload={"sizing_context": {"weight_precision": 0.01}},
    )
    _append_artifact(
        database,
        "sizing-context",
        ExecutionArtifactType.TARGET_SNAPSHOT,
        make_artifact_reference(view="targets", input_id=frozen["audit_id"]),
    )
    record = persist_result(
        database,
        {"symbol_results": {"rb2610": {"sizing": {"reason_code": "SIZED", "target_quantity": 2}}}},
        execution_id="sizing-context",
        is_success=1,
        raw_input=terminal_input,
    )
    snapshot = TargetWeightSnapshotData(
        id=1,
        portfolio_id=1,
        account_id=1,
        raw_weights={"rb2610": 1},
        normalized_weights={"rb2610": 1},
        source="execution",
        execution_id=record["execution_id"],
        calculated_at=record["created_at"],
    )
    evidence = asyncio.run(
        account_execution._sizing_evidence_for_snapshot(
            snapshot,
            canonicalize=lambda symbol: symbol,
            strategy_weights={"rb2610": 1},
            account_weights={"rb2610": 1},
        )
    )
    assert evidence.status == "available"
    assert evidence.rows["rb2610"].weight_precision == 0.01
    assert asyncio.run(read_execution_input(record["input_id"])) == terminal_input


@pytest.mark.parametrize("delete_account", [False, True])
def test_delete_execution_records_preserves_independent_summary_journal(database, monkeypatch, delete_account):
    """真实引用经应用删除后仍可读，且结果事实级联删除不影响其他账户。"""

    async def drop_worker(_account_id):
        return None

    monkeypatch.setattr(account_crud, "delete_job", lambda _sched, _account_id: None)
    monkeypatch.setattr(account_crud, "drop_account_worker", drop_worker)
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
    journal = {**deepcopy(result), "journal_note": {"independent": True}}
    _append_artifact(database, "delete-summary", ExecutionArtifactType.EXECUTION_SUMMARY, deepcopy(journal))
    record = persist_result(
        database, result, execution_id="delete-summary", is_success=1, raw_input={"curr_target": {"rb2610": 1}}
    )
    other = persist_result(database, {}, account_id=2, execution_id="keep-other", is_success=1)
    artifact = next(row for row in rows(database, "execution_artifact") if row["execution_id"] == "delete-summary")
    assert artifact["content"] == journal

    if delete_account:
        message = asyncio.run(account_crud.delete_account(sched=SimpleNamespace(), account_id=1))
        assert message.message == "成功删除账户"
    else:
        message = asyncio.run(account_crud.delete_execute_records(account_id=1))
        assert message.message == "成功删除"
    remaining = {row["id"] for row in rows(database, "executerecord")}
    assert record["id"] not in remaining
    assert other["id"] in remaining
    assert not [row for row in rows(database, "execution_extension") if row["record_id"] == record["id"]]
    artifact = next(row for row in rows(database, "execution_artifact") if row["id"] == artifact["id"])
    assert artifact["schema_version"] == 2
    assert "business_facts_ref" not in artifact["content"]
    assert business_evidence(asyncio.run(resolve_artifact_content(artifact, account_id=1))) == business_evidence(
        journal
    )
