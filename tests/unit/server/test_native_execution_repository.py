"""业务事实读入口只传身份，并保留原有领域错误语义。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.server._native import _core
from axile.server.business_facts import repository


@pytest.mark.parametrize("mapping", [False, True])
def test_execution_detail_reads_one_native_snapshot(monkeypatch, mapping):
    expected = {"id": 7, "input": {"curr_target": {}}, "result": {"status": "FAILED"}}
    native = AsyncMock(return_value=expected)
    monkeypatch.setattr(repository, "native_operation", native)
    identity = {"id": 7, "account_id": 3}
    record = identity if mapping else SimpleNamespace(**identity)
    assert asyncio.run(repository.read_execution_detail(record)) is expected
    native.assert_awaited_once_with("read_execution_detail", {"record_id": 7, "account_id": 3})


def test_invalid_record_identity_does_not_enter_native_storage(monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr(repository, "native_operation", native)
    with pytest.raises(ValueError, match="主键或账户身份"):
        asyncio.run(repository.read_execution_result({"id": True, "account_id": 3}))
    native.assert_not_awaited()


def test_missing_input_keeps_value_error_contract(monkeypatch):
    native = AsyncMock(side_effect=_core.NativeError("BUSINESS_FACTS", "执行输入不存在"))
    monkeypatch.setattr(repository, "native_operation", native)
    with pytest.raises(ValueError, match="执行输入不存在"):
        asyncio.run(repository.read_execution_input("missing"))


def test_storage_failures_keep_native_error_identity(monkeypatch):
    error = _core.NativeError("DATABASE_OPEN", "unavailable")
    monkeypatch.setattr(repository, "native_operation", AsyncMock(side_effect=error))
    with pytest.raises(_core.NativeError) as raised:
        asyncio.run(repository.read_execution_input("frozen"))
    assert raised.value is error
