"""规范执行事实写入的事务、脱敏和下单前失败边界。"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from axile.domain.execution import ExecutionTaskStatus
from axile.server._native import _core
from axile.server.business_facts.summary_refs import BusinessFactReferenceError
from axile.server.execution import input_persistence, records
from axile.server.execution.worker_backend import worker_audit
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account


def test_pre_order_commit_hands_full_request_to_native_freeze(monkeypatch):
    captured: dict[str, object] = {}

    def freeze(command, request):
        captured.update(command=command, request=request)
        return {"input_id": "durable-input"}

    monkeypatch.setattr(input_persistence, "_freeze_facts", freeze)
    payload = {
        "account_config": {"password": "synthetic"},
        "execution_notification_code": "synthetic-source",
        "feishu_key": "synthetic-address",
        "curr_target": {"A": 0.0},
        "last_target": {},
        "execution_timeout": None,
    }
    before = {"total_asset": 0.0, "positions": []}
    input_id = asyncio.run(
        asyncio.to_thread(
            input_persistence.persist_pre_execute_input_sync,
            account_id=1,
            execution_id="before-final",
            payload=payload,
            before_account_assets=before,
        )
    )
    request = captured["request"]
    assert captured["command"] == "freeze_execution_input"
    assert business_evidence(input_id) == business_evidence("durable-input")
    # 原始负载原样交原生事务脱敏,调用侧对象不被修改。
    assert request["payload"] is payload
    assert request["before_assets"] is before
    assert request["execution_id"] == "before-final"
    assert "audit_payload" not in request
    assert "account_config" in payload


def test_clear_pre_order_failure_prevents_later_audit_and_submit(monkeypatch):
    reached: list[str] = []

    def fail_required_input(**_kwargs):
        raise RuntimeError("cannot commit input")

    monkeypatch.setattr(worker_audit, "persist_pre_execute_input_sync", fail_required_input)
    monkeypatch.setattr(worker_audit, "append_execution_audit_batch_sync", lambda _batch: reached.append("batch"))
    monkeypatch.setattr(worker_audit, "append_execution_event_sync", lambda **_kwargs: reached.append("event"))
    with pytest.raises(RuntimeError, match="cannot commit input"):
        worker_audit._append_empty_positions_pre_execute_audit(
            account=build_account(),
            execution_id="clear-before-final",
            algorithm_name="SINGLE-MAKER",
            executor=object(),
            audit_input={"algorithm": {"method": "SINGLE-MAKER"}},
        )
        reached.append("submit")
    assert business_evidence(reached) == business_evidence([])


def test_post_order_assets_commit_is_required(monkeypatch):
    def fail_freeze(_command, _request):
        raise RuntimeError("cannot commit after assets")

    monkeypatch.setattr(input_persistence, "_freeze_facts", fail_freeze)
    with pytest.raises(RuntimeError, match="cannot commit after assets"):
        input_persistence.persist_post_execute_assets_sync(
            account_id=1, execution_id="after-before-final", payload={"account_assets": {"total_asset": 0.0}}
        )


def test_terminal_facts_use_durable_input_and_commit_once(native_database, monkeypatch):
    import json

    from axile.server.core import db
    from tests.unit.server._business_facts_test_support import seed_account
    from tests.unit.server._native_read_views import receipt_record

    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    seed_account(native_database)
    native = AsyncMock(wraps=records.native_operation)
    monkeypatch.setattr(records, "native_operation", native)
    monkeypatch.setattr(records, "now_str", lambda: "frozen-time")
    raw_input = {"curr_target": {"A": 0.1}, "account_config": {"secret": "synthetic"}}
    result = {
        "status": "SUCCEEDED",
        "task_status": "SUCCEEDED",
        "inputs": {"curr_target": {}, "account_config": {"secret": "synthetic"}, "feishu_key": "synthetic"},
    }
    _core.freeze_execution_input(
        str(native_database),
        json.dumps(
            {
                "account_id": 1,
                "execution_id": "terminal",
                "input_id": "durable-normalized-input",
                "created_at": "frozen-time",
                "payload": raw_input,
            }
        ),
    )
    record = asyncio.run(
        records._persist_execute_record(
            account_id=1,
            execution_id="terminal",
            raw_input=raw_input,
            raw_result=result,
            is_success=1,
        )
    )
    assert receipt_record(record)["input_id"] == "durable-normalized-input"
    native.assert_awaited_once()
    command, request = native.await_args.args
    assert command == "persist_execution_state"
    assert request["raw_input"] is raw_input
    assert request["raw_result"] is result
    assert request["created_at"] == "frozen-time"
    assert request["account_id"] == 1
    assert request["execution_id"] == "terminal"
    assert uuid.UUID(request["input_id"]) != uuid.UUID(request["result_input_id"])
    assert business_evidence(record["status"]) == business_evidence("SUCCEEDED")
    assert "account_config" in result["inputs"]


def test_duplicate_terminal_never_returns_another_accounts_record(monkeypatch):
    monkeypatch.setattr(
        records,
        "native_operation",
        AsyncMock(side_effect=_core.NativeError("BUSINESS_FACTS", "执行标识属于其他账户")),
    )
    with pytest.raises(ValueError, match="其他账户"):
        asyncio.run(
            records._persist_execute_record(
                account_id=1,
                execution_id="owned-elsewhere",
                raw_input=None,
                raw_result={"status": "FAILED"},
                is_success=0,
            )
        )


def test_journal_compaction_failure_aborts_terminal_commit(monkeypatch):
    failure = _core.NativeError("BUSINESS_FACTS_SQL", "journal failure")
    native = AsyncMock(side_effect=failure)
    monkeypatch.setattr(records, "native_operation", native)
    with pytest.raises(_core.NativeError, match="journal failure"):
        asyncio.run(
            records._persist_execute_record(
                account_id=1,
                execution_id="journal-atomic",
                raw_input=None,
                raw_result={"status": "SUCCEEDED"},
                is_success=1,
            )
        )
    native.assert_awaited_once()


def test_invalid_journal_reference_keeps_domain_error(monkeypatch):
    monkeypatch.setattr(
        records,
        "native_operation",
        AsyncMock(side_effect=_core.NativeError("BUSINESS_FACTS_REFERENCE", "执行摘要缺少结果引用")),
    )
    with pytest.raises(BusinessFactReferenceError, match="执行摘要缺少结果引用"):
        asyncio.run(
            records._persist_execute_record(
                account_id=1, execution_id="invalid-ref", raw_input=None, raw_result={}, is_success=0
            )
        )


@pytest.mark.parametrize("status", ["SKIPPED", "PARTIAL", "TERMINATED"])
def test_task_status_uses_canonical_terminal_column(status, native_database):
    import json

    from tests.unit.server._business_facts_test_support import seed_account

    seed_account(native_database)
    receipt = _core.persist_execution_state(
        str(native_database),
        json.dumps(
            {
                "account_id": 1,
                "execution_id": "terminal-column",
                "input_id": "input",
                "result_input_id": "result",
                "created_at": "frozen",
                "is_success": 0,
                "raw_input": {},
                "raw_result": {"task_status": status},
            }
        ),
    )
    assert business_evidence(receipt["status"]) == business_evidence(ExecutionTaskStatus(status))
