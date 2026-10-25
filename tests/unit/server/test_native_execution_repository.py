"""业务事实读入口只传身份，并保留原有领域错误语义。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from axile.domain.execution import ExecutionTaskStatus
from axile.server._native import _core
from axile.server.business_facts.summary_refs import BusinessFactReferenceError
from axile.server.execution import intents, records


@pytest.mark.parametrize("boundary", [records, intents])
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("BUSINESS_FACTS", ValueError),
        ("BUSINESS_FACTS_REFERENCE", BusinessFactReferenceError),
        ("DATABASE_OPEN", _core.NativeError),
    ],
)
def test_business_fact_error_contract_is_shared(monkeypatch, boundary, code, expected):
    error = _core.NativeError(code, "native failure")
    monkeypatch.setattr(boundary, "native_operation", AsyncMock(side_effect=error))

    async def invoke():
        if boundary is records:
            return await records._persist_execute_record(
                account_id=1, execution_id="failed", raw_input=None, raw_result={}, is_success=0
            )
        return await intents.mark_intent_finished("failed", ExecutionTaskStatus.FAILED)

    with pytest.raises(expected, match="native failure") as raised:
        asyncio.run(invoke())
    assert type(raised.value) is expected
    if expected is _core.NativeError:
        assert raised.value is error


def test_intent_boundary_does_not_translate_non_native_exceptions(monkeypatch):
    error = ValueError("SUPPLEMENT_NOT_RUNNABLE", "not a native error")
    monkeypatch.setattr(intents, "native_operation", AsyncMock(side_effect=error))
    with pytest.raises(ValueError) as raised:
        asyncio.run(intents.mark_intent_finished("failed", ExecutionTaskStatus.FAILED))
    assert raised.value is error


def test_terminal_receipt_is_final_ordered_and_first_write_wins(native_database):
    import json

    from tests.unit.server._business_facts_test_support import seed_account

    seed_account(native_database)
    request = {
        "account_id": 1,
        "execution_id": "receipt",
        "is_success": 0,
        "created_at": "frozen",
        "input_id": "receipt-input",
        "result_input_id": "receipt-result",
        "raw_input": {},
        "raw_result": {
            "status": "PARTIAL",
            "task_status": "PARTIAL",
            "error": "未到位",
            "symbol_results": {"second": {"status": "BLOCKED"}, "first": {"status": "PARTIAL"}},
        },
    }
    first = _core.persist_execution_state(str(native_database), json.dumps(request))
    request["raw_result"] = {"status": "FAILED", "error": "later"}
    second = _core.persist_execution_state(str(native_database), json.dumps(request))
    assert first == second
    assert first["status"] == first["output_status"] == "PARTIAL"
    assert first["outcome_symbols"] == ["second", "first"]
    assert first["error"] == "未到位" and first["finished_at"] is None
    assert first["record_id"] > 0 and "id" not in first and "input_id" not in first
    assert not hasattr(_core, "persist_execute_record")
    assert not hasattr(_core, "submission_monitor")
