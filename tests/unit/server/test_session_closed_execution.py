"""休市跳过保留未完成证据，并贯通落库、任务终态和审计回放。"""

import asyncio
from unittest.mock import Mock

import pytest

from axile.domain.execution import ExecutionTaskStatus
from axile.executor.session_closed import COMMON_SESSION_CLOSED, session_closed_scope
from axile.server.business_facts.head import execution_head_values
from axile.server.business_facts.repository import decode_result_facts, encode_result_facts
from axile.server.db.models import ExecuteRecord
from axile.server.execution import lifecycle, records, registry


def closed_result():
    return {
        "status": "BLOCKED",
        "reason_code": COMMON_SESSION_CLOSED,
        "error": "非交易时段",
        "success": False,
        "symbol_results": {
            "m2701": {
                "status": "BLOCKED",
                "reason_code": COMMON_SESSION_CLOSED,
                "target_volume": -38,
                "final_volume": None,
                "orders": [],
                "trades": [],
            }
        },
    }


def _record(result, *, execution_id="session-closed-record", is_success=0):
    """通过正式头字段转换构造任务回放所读取的紧凑记录。"""
    return ExecuteRecord(
        id=4,
        account_id=1,
        execution_id=execution_id,
        created_at="2026-10-16T10:15:07",
        is_success=is_success,
        **execution_head_values(result, is_success=is_success),
    )


@pytest.mark.parametrize("failure", ["FAILED", "PARTIAL", "BLOCKED"])
def test_real_errors_alongside_closed_symbols_are_not_session_skips(failure):
    raw = closed_result()
    raw["symbol_results"]["ag2612"] = {"status": failure, "reason_code": "CHANNEL.QUERY_FAILED"}
    assert session_closed_scope(raw) is None
    record = _record(raw)
    assert lifecycle._resolve_completion_status(record) == ExecutionTaskStatus.FAILED


def test_order_evidence_and_termination_prevent_session_skip():
    raw = closed_result()
    raw["symbol_results"]["m2701"]["orders"] = [{"order_id": "pending"}]
    assert session_closed_scope(raw) is None
    raw = closed_result()
    raw["orders"] = [{"order_id": "legacy-pending"}]
    assert session_closed_scope(raw) is None
    raw = closed_result()
    raw["task_status"] = "TERMINATED"
    assert session_closed_scope(raw) is None
    assert session_closed_scope({"status": "BLOCKED", "error": "非交易时段"}) is None


def test_order_evidence_prevents_a_neutral_session_title_in_lightweight_results():
    from axile.common.trade_channel import TradeChannel
    from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus
    from axile.executor.models.unified_account_assets import UnifiedAccountAssets
    from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
    from axile.executor.models.unified_output import UnifiedStandardOutput

    order = UnifiedOrder(
        order_id="pending",
        symbol="m2701",
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        volume=1,
        price=100,
        status="SUBMITTED",
    )
    output = UnifiedStandardOutput(
        status=ExecutionStatus.BLOCKED,
        channel_type=TradeChannel.TQ,
        account_assets=UnifiedAccountAssets.unavailable(),
        symbol_results={
            "m2701": AlgorithmResult(
                symbol="m2701", status=ExecutionStatus.BLOCKED, reason_code=COMMON_SESSION_CLOSED, orders=[order]
            )
        },
    )
    assert output.reason_code is None
    assert session_closed_scope(output.model_dump(mode="json")) is None


def test_closed_and_open_noop_is_partial_and_keeps_unhandled_target():
    raw = closed_result()
    raw["status"] = "PARTIAL"
    raw["symbol_results"]["ag2612"] = {"status": "NOOP", "final_volume": -6, "target_volume": -6}
    assert session_closed_scope(raw) == "partial"
    assert lifecycle._resolve_completion_status(_record(raw)) == ExecutionTaskStatus.PARTIAL


@pytest.mark.parametrize("partial", [False, True])
def test_session_skip_persistence_and_status_replay_do_not_claim_success(monkeypatch, partial):
    captured = {}

    async def persist(**kwargs):
        captured.update(kwargs)
        batch = encode_result_facts(4, kwargs["raw_result"], is_success=kwargs["is_success"])
        captured["result"] = decode_result_facts({table.name: rows for table, rows in batch.items()})
        return _record(kwargs["raw_result"], execution_id=kwargs["execution_id"], is_success=kwargs["is_success"])

    monkeypatch.setattr(records, "_persist_execute_record", persist)
    logger = Mock()
    raw = closed_result()
    expected = ExecutionTaskStatus.SKIPPED
    if partial:
        raw["status"] = "PARTIAL"
        raw["outcome_reason"] = None
        raw["symbol_results"]["ag2612"] = {"status": "NOOP", "target_volume": -6, "final_volume": -6}
        expected = ExecutionTaskStatus.PARTIAL
    record = asyncio.run(
        records.append_error_execute_record(
            account_id=1,
            execution_id="session-skip",
            raw_result=raw,
            msg="非交易时段",
            logger=logger,
        )
    )
    assert captured["is_success"] == 0
    result = captured["result"]
    assert result["task_status"] == expected
    assert result["outcome_reason"] == "非交易时段"
    assert result["status"] == raw["status"]
    assert result["symbol_results"]["m2701"]["final_volume"] is None
    assert not hasattr(record, "raw_result")
    assert lifecycle._resolve_completion_status(record) == expected
    replay = registry._build_persisted_execution_status(record)
    assert replay["status"] == expected
    assert replay["output_status"] == raw["status"]
    assert replay["is_success"] == 0
    logger.error.assert_not_called()
    logger.info.assert_called_once()


@pytest.mark.parametrize("terminal", [ExecutionTaskStatus.SKIPPED, ExecutionTaskStatus.PARTIAL])
def test_skipped_task_is_terminal_and_cannot_be_terminated_again(terminal):
    from axile.domain.execution import ExecutionKind, ExecutionTerminateMode

    execution_id = "session-skip-terminal"
    state = registry.ExecutionTaskState(
        execution_id=execution_id,
        account_id=1,
        execution_kind=ExecutionKind.REBALANCE,
        status=terminal,
        created_at="2026-10-16T10:15:00",
        finished_at="2026-10-16T10:15:07",
    )
    registry.set_execution_task_state(execution_id, state)
    try:
        _, changed = registry.request_execution_termination(
            execution_id,
            reason="late stop",
            mode=ExecutionTerminateMode.GRACEFUL,
        )
        assert not changed
        assert registry.get_execution_task_state(execution_id).status == terminal
    finally:
        registry._clear_execution_task_state(execution_id)
