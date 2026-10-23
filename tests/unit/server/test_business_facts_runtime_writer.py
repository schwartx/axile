"""规范执行事实写入的事务、脱敏和下单前失败边界。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from axile.domain.execution import ExecutionTaskStatus
from axile.server.db.models import ExecuteRecord
from axile.server.execution import input_persistence, records
from axile.server.execution.worker_backend import worker_audit
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account


class _WriteSession:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.existing: object | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def add(self, record):
        record.id = 101
        self.events.append("add")

    async def flush(self):
        self.events.append("flush")

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")

    async def refresh(self, _record):
        self.events.append("refresh")

    async def scalar(self, _statement):
        return self.existing


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
        input_persistence._persist_pre_execute_input(
            account_id=1, execution_id="before-final", payload=payload, before_account_assets=before
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


def test_terminal_facts_use_durable_input_and_commit_once(monkeypatch):
    session = _WriteSession()
    seen: dict[str, object] = {}

    async def store(_session, record, *, raw_input, raw_result):
        session.events.append("facts")
        record.input_id = "durable-normalized-input"
        seen.update(input=raw_input, result=raw_result)

    async def asset_history(_session, _record):
        session.events.append("asset_history")

    async def compact_journal(_session, _record, *, result):
        assert business_evidence(result) == business_evidence(seen["result"])
        session.events.append("compact_journal")

    monkeypatch.setattr(records, "store_execution_facts", store)
    monkeypatch.setattr(records, "ensure_execution_asset_snapshot", asset_history)
    monkeypatch.setattr(records, "compact_execution_summary", compact_journal)
    result = {
        "status": "SUCCEEDED",
        "task_status": "SUCCEEDED",
        "inputs": {"curr_target": {}, "account_config": {"secret": "synthetic"}, "feishu_key": "synthetic"},
    }
    record = asyncio.run(
        records._persist_execute_record(
            account_id=1,
            execution_id="terminal",
            raw_input={"curr_target": {"A": 0.1}, "account_config": {"secret": "synthetic"}},
            raw_result=result,
            is_success=1,
            session_factory=lambda: session,
        )
    )
    assert record.input_id == "durable-normalized-input"
    assert business_evidence(seen["input"]) == business_evidence({"curr_target": {"A": 0.1}})
    assert business_evidence(seen["result"]["inputs"]) == business_evidence({"curr_target": {}, "feishu_key": "***"})
    assert business_evidence(session.events) == business_evidence(
        [
            "add",
            "flush",
            "facts",
            "asset_history",
            "compact_journal",
            "flush",
            "commit",
            "refresh",
        ]
    )
    assert business_evidence(record.task_status) == business_evidence("SUCCEEDED")
    assert "raw_input" not in record.__class__.__table__.c
    assert "raw_result" not in record.__class__.__table__.c
    assert "account_config" in result["inputs"]


def test_duplicate_terminal_never_returns_another_accounts_record(monkeypatch):
    session = _WriteSession()
    session.existing = SimpleNamespace(account_id=2, id=99)
    monkeypatch.setattr(
        records,
        "store_execution_facts",
        AsyncMock(side_effect=IntegrityError("insert", {}, RuntimeError("duplicate"))),
    )
    with pytest.raises(ValueError, match="其他账户"):
        asyncio.run(
            records._persist_execute_record(
                account_id=1,
                execution_id="owned-elsewhere",
                raw_input=None,
                raw_result={"status": "FAILED"},
                is_success=0,
                session_factory=lambda: session,
            )
        )
    assert "rollback" in session.events
    assert "commit" not in session.events


def test_journal_compaction_failure_aborts_terminal_commit(monkeypatch):
    session = _WriteSession()
    monkeypatch.setattr(records, "store_execution_facts", AsyncMock())
    monkeypatch.setattr(records, "ensure_execution_asset_snapshot", AsyncMock())
    monkeypatch.setattr(records, "compact_execution_summary", AsyncMock(side_effect=RuntimeError("journal failure")))
    with pytest.raises(RuntimeError, match="journal failure"):
        asyncio.run(
            records._persist_execute_record(
                account_id=1,
                execution_id="journal-atomic",
                raw_input=None,
                raw_result={"status": "SUCCEEDED"},
                is_success=1,
                session_factory=lambda: session,
            )
        )
    assert "commit" not in session.events


@pytest.mark.parametrize("status", ["SKIPPED", "PARTIAL", "TERMINATED"])
def test_task_status_uses_canonical_terminal_column(status):
    record = ExecuteRecord(account_id=1, is_success=0, task_status=status)
    assert business_evidence(records.resolve_record_task_status(record)) == business_evidence(
        ExecutionTaskStatus(status)
    )
