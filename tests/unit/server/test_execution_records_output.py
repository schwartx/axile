"""执行输出落库：受约束尝试与故障共享未成功事实，但保留不同状态。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.server.db.models import ExecuteRecord
from axile.server.execution import records
from axile.server.execution.records import append_execute_record_from_output
from tests.unit.server._business_facts_test_support import seed_account
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._native_read_views import read_execution_result


def _output(*, status: ExecutionStatus, error: str | None, success: bool) -> UnifiedStandardOutput:
    return UnifiedStandardOutput(
        account_assets=UnifiedAccountAssets(available_cash=1.0, total_asset=1.0, market_value=0.0, positions=[]),
        memory={},
        inputs=None,
        execution_time=0.1,
        channel_type=TradeChannel.TQ,
        symbol_results={},
        status=status,
        error=error,
        success=success,
    )


def test_blocked_output_persists_as_unsuccessful_attempt(monkeypatch) -> None:
    """全员 BLOCKED 未完成执行，保留 ``is_success=0`` 与输出状态。"""
    captured: dict[str, object] = {}

    async def fake_success(account, raw_input, result, execution_id=None, execution_kind=None):
        captured["path"] = "success"
        captured["execution_kind"] = execution_kind
        captured["result_status"] = result.get("status")
        return SimpleNamespace(id=11, is_success=1, status=result.get("status"))

    async def fake_error(**kwargs: object):
        captured["path"] = "error"
        captured["msg"] = kwargs.get("msg")
        return SimpleNamespace(id=12, is_success=0)

    monkeypatch.setattr(
        "axile.server.execution.records.append_success_execute_record",
        fake_success,
    )
    monkeypatch.setattr(
        "axile.server.execution.records.append_error_execute_record",
        fake_error,
    )

    output = _output(status=ExecutionStatus.BLOCKED, error="rb2610 因交易时段不可执行", success=False)
    record = asyncio.run(
        append_execute_record_from_output(
            account=build_account(),
            raw_input={"curr_target": {"rb2610": 0.1}},
            output=output,
            execution_id="exec-blocked-persist",
            execution_kind=ExecutionKind.REBALANCE,
        )
    )
    assert captured["path"] == "error"
    assert captured["msg"] == "rb2610 因交易时段不可执行"
    assert record.is_success == 0


def test_failed_output_still_persists_as_error(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_success(*_args: object, **_kwargs: object):
        captured["path"] = "success"
        return SimpleNamespace(id=11, is_success=1)

    async def fake_error(**kwargs: object):
        captured["path"] = "error"
        captured["msg"] = kwargs.get("msg")
        return SimpleNamespace(id=12, is_success=0)

    monkeypatch.setattr(
        "axile.server.execution.records.append_success_execute_record",
        fake_success,
    )
    monkeypatch.setattr(
        "axile.server.execution.records.append_error_execute_record",
        fake_error,
    )

    output = _output(status=ExecutionStatus.FAILED, error="下单失败", success=False)
    record = asyncio.run(
        append_execute_record_from_output(
            account=build_account(),
            raw_input={},
            output=output,
            execution_id="exec-failed-persist",
            execution_kind=ExecutionKind.REBALANCE,
        )
    )
    assert captured["path"] == "error"
    assert captured["msg"] == "下单失败"
    assert record.is_success == 0


def test_not_reached_clear_keeps_conclusion_and_kind_on_error_record_path(native_database, monkeypatch):
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    seed_account(native_database)
    output = _output(status=ExecutionStatus.FAILED, error="目标未完成", success=False)
    output.status = ExecutionStatus.PARTIAL
    output.error = "剩余 1 手"

    async def scenario():
        record = await append_execute_record_from_output(
            account=build_account(id=1),
            raw_input={},
            output=output,
            execution_id="clear-shortfall",
            execution_kind=ExecutionKind.CLEAR_POSITIONS,
        )
        return record, await read_execution_result(record)

    record, result = asyncio.run(scenario())
    assert isinstance(result, dict)
    assert result["status"] == "PARTIAL"
    assert result["error"] == "剩余 1 手"
    # 柔和版落库边界按当前 status 重新派生 outcome，两套结论不分离。
    assert result["outcome"] == "not_reached"
    assert result["execution_kind"] == "clear_positions"
    assert record.status == "PARTIAL"
    assert record.outcome == "not_reached"
    assert record.execution_kind == "clear_positions"
    assert record.is_success == 0


@pytest.mark.parametrize(
    ("account_id", "execution_id", "expected_calls"),
    [(1, "failed", 1), (2, "failed", 2), (1, "other", 2), (1, None, 2)],
)
def test_error_record_reuse_is_scoped_to_execution(monkeypatch, account_id, execution_id, expected_calls):
    record = ExecuteRecord(id=7, account_id=1, execution_id="failed", is_success=0)
    persist = AsyncMock(return_value=record)
    monkeypatch.setattr(records, "_persist_execute_record", persist)
    cause = RuntimeError("SDK failure")
    wrapped = ValueError("execution failure")
    wrapped.__cause__ = cause

    async def run():
        assert await records.append_error_execute_record(account_id=1, execution_id="failed", error=cause) is record
        assert (
            await records.append_error_execute_record(account_id=account_id, execution_id=execution_id, error=wrapped)
            is record
        )

    asyncio.run(run())
    assert persist.await_count == expected_calls


def test_failed_error_record_commit_is_retried_without_hanging_on_cause_cycle(monkeypatch):
    record = ExecuteRecord(id=7, account_id=1, execution_id="failed", is_success=0)
    persist = AsyncMock(side_effect=[RuntimeError("commit failed"), record])
    monkeypatch.setattr(records, "_persist_execute_record", persist)
    error = ValueError("execution failure")
    error.__cause__ = error

    async def run():
        with pytest.raises(RuntimeError, match="commit failed"):
            await records.append_error_execute_record(account_id=1, execution_id="failed", error=error)
        assert await records.append_error_execute_record(account_id=1, execution_id="failed", error=error) is record

    asyncio.run(run())
    assert persist.await_count == 2
