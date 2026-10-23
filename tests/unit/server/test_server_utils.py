"""服务端通用工具函数测试。"""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server import channel_capabilities
from axile.server import utils as server_utils
from axile.server.business_facts.repository import read_execution_detail
from axile.server.db.models import Account
from axile.server.db.models.account import AccountUpdate
from axile.server.execution import records as execution_records
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import insert_account


def _build_account() -> Account:
    return Account(
        id=1,
        name="ctp-testnet-sim",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_control_preset="default",
        account_control_override=None,
        account_config={
            "broker_id": "9999",
            "investor_id": "test",
            "password": "test",
            "td_front": "tcp://td:1",
            "md_front": "tcp://md:2",
            "app_id": "app",
            "auth_code": "auth",
        },
        is_started=True,
        cron_expr="*/5 * * * *",
        remark=None,
        brokerage="ctp",
        weight_precision=0.001,
        long_leverage=1.0,
        short_leverage=1.0,
        algorithm={"method": "SINGLE-MAKER"},
        empty_positions_algorithm=None,
        trade_rules={},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        portfolio_id=None,
        write_empty_record=0,
    )


@pytest.fixture
def record_database(native_database: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    with closing(sqlite3.connect(native_database)) as connection, connection:
        insert_account(connection, _build_account())
    return native_database


@pytest.mark.parametrize("timeout", [0, -1, -180])
def test_account_update_rejects_non_positive_execution_timeout(timeout: int) -> None:
    """0 / 负数应被拒绝：不允许按账户关掉「执行无限挂起」这道兜底。"""
    with pytest.raises(ValidationError):
        AccountUpdate(execution_timeout=timeout)


@pytest.mark.parametrize("timeout", [541, 600, 3600])
def test_account_update_rejects_execution_timeout_above_worker_kill_deadline(timeout: int) -> None:
    """超过上界应被拒绝：否则多进程后端 600s 的强杀兜底会先于协作撤单触发。"""
    with pytest.raises(ValidationError):
        AccountUpdate(execution_timeout=timeout)


def test_append_error_execute_record_sanitizes_account_config(record_database: Path) -> None:
    record = asyncio.run(
        execution_records.append_error_execute_record(
            account_id=1,
            raw_input={"account_config": {"secret": "x"}, "symbol": "rb2610"},
            msg="boom",
        )
    )
    detail = asyncio.run(read_execution_detail(record))

    assert detail["id"] == record.id
    assert business_evidence(detail["input"], context="input") == business_evidence(
        {"symbol": "rb2610"}, context="input"
    )
    assert detail["result"] == {
        "msg": "boom",
        "status": "FAILED",
        "error": "boom",
        # 柔和版保留展示结论字段，与 status/error 同写。
        "outcome": "error",
        "outcome_reason": "boom",
        "task_status": "FAILED",
    }
    assert record.status == "FAILED"
    assert record.error == "boom"
    assert record.is_success == 0


def test_append_terminated_execute_record_persists_termination_metadata(record_database: Path) -> None:
    record = asyncio.run(
        execution_records.append_terminated_execute_record(
            account_id=1,
            execution_id="exec-1",
            execution_kind=ExecutionKind.CLEAR_POSITIONS,
            reason="manual stop",
            mode="graceful",
            requested_at="2026-03-22T18:00:00",
            acked_at="2026-03-22T18:00:01",
            finished_at="2026-03-22T18:00:02",
            cancel_attempted=True,
            cancel_failed_order_ids=["oid-1"],
            raw_input={"account_config": {"secret": "x"}, "symbol": "ag2612"},
            raw_result={"summary": {"filled": 0}},
        )
    )
    detail = asyncio.run(read_execution_detail(record))

    assert detail["id"] == record.id
    assert business_evidence(detail["input"], context="input") == business_evidence(
        {"symbol": "ag2612"}, context="input"
    )
    result = detail["result"]
    assert result["task_status"] == ExecutionTaskStatus.TERMINATED.value
    assert result["execution_kind"] == ExecutionKind.CLEAR_POSITIONS.value
    assert result["termination"]["cancel_failed_order_ids"] == ["oid-1"]
    assert record.task_status == ExecutionTaskStatus.TERMINATED.value
    assert record.termination_reason == "manual stop"


def test_trade_channel_check_records_error_when_dependency_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    appended: list[tuple[int | None, str]] = []

    async def fake_append_error_execute_record(*, account_id: int | None, msg: str, **_kwargs: object) -> object:
        appended.append((account_id, msg))
        return object()

    monkeypatch.setattr(
        channel_capabilities.importlib.util,
        "find_spec",
        lambda name: None if name == "openctp_ctp" else object(),
    )
    monkeypatch.setattr(execution_records, "append_error_execute_record", fake_append_error_execute_record)

    with pytest.raises(ValueError, match="请先安装 CTP 对应的依赖"):
        asyncio.run(server_utils.trade_channel_check(_build_account()))

    assert appended == [(1, "请先安装 CTP 对应的依赖")]
