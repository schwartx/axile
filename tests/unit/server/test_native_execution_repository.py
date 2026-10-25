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
