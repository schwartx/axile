"""结构化业务事实在真实落库、任务回放和下一次调仓中的语义回归。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.session_closed import COMMON_SESSION_CLOSED
from axile.server.business_facts import assets
from axile.server.business_facts.repository import read_execution_input, read_execution_result
from axile.server.db.models import ExecuteRecord
from axile.server.execution import execution_records_output, input_persistence, lifecycle, rebalance, records, registry
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """为每个场景建立独立数据库，避免读到运行服务或其他工作树的数据。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(build_account(id=1, portfolio_id=None, account_config={}))
            session.add(build_account(id=2, portfolio_id=None, account_config={}))
            await session.commit()
        yield factory
    finally:
        await engine.dispose()


@asynccontextmanager
async def _file_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path, name: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """文件数据库:原生冻结入口另开写连接,须与异步引擎共用同一文件。"""
    path = tmp_path / f"{name}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(build_account(id=1, portfolio_id=None, account_config={}))
            session.add(build_account(id=2, portfolio_id=None, account_config={}))
            await session.commit()
        monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
        yield factory
    finally:
        await engine.dispose()


def _input(target: object) -> dict[str, object]:
    return {"channel_type": "TQ", "curr_target": target, "last_target": {}, "algorithm": {"method": "TEST"}}


def _session_closed_result(*, partial: bool) -> dict[str, object]:
    symbols: dict[str, object] = {
        "closed": {
            "status": "BLOCKED",
            "reason_code": COMMON_SESSION_CLOSED,
            "target_volume": 0,
            "final_volume": None,
            "orders": [],
            "trades": [],
        }
    }
    if partial:
        symbols["open"] = {"status": "NOOP", "target_volume": 0, "final_volume": 0, "orders": [], "trades": []}
    return {
        "status": "PARTIAL" if partial else "BLOCKED",
        "reason_code": COMMON_SESSION_CLOSED,
        "error": "非交易时段",
        "success": False,
        "symbol_results": symbols,
    }


@pytest.mark.parametrize(
    ("output_status", "task_status", "outcome", "is_success", "kind", "session_scope"),
    [
        ("SUCCEEDED", "SUCCEEDED", "completed", 1, "rebalance", None),
        ("NOOP", "SUCCEEDED", "completed", 1, "rebalance", None),
        ("SUCCEEDED", "SUCCEEDED", "completed", 1, "clear_positions", None),
        ("FAILED", "FAILED", "error", 0, "rebalance", None),
        ("PARTIAL", "FAILED", "not_reached", 0, "clear_positions", None),
        ("BLOCKED", "FAILED", "blocked", 0, "rebalance", None),
        ("BLOCKED", "SKIPPED", "blocked", 0, "rebalance", "all"),
        ("PARTIAL", "PARTIAL", "not_reached", 0, "rebalance", "partial"),
    ],
)
def test_terminal_facts_keep_output_lifecycle_and_outcome_distinct(
    output_status: str,
    task_status: str,
    outcome: str,
    is_success: int,
    kind: str,
    session_scope: str | None,
) -> None:
    """持久化与重启回放保留三套各有用途的状态，休市不制造执行成功。"""

    async def scenario() -> None:
        async with _database() as factory:
            session_factory = cast("records.SessionFactory", factory)
            result = {
                "status": output_status,
                "outcome": outcome,
                "outcome_reason": None if is_success else "目标未完成",
                "error": None if is_success else "目标未完成",
                "success": bool(is_success),
                "execution_kind": kind,
                "channel_type": "TQ",
                "execution_time": 0,
                "symbol_results": {},
            }
            if session_scope:
                result.update(_session_closed_result(partial=session_scope == "partial"))
            if is_success:
                record = await records.append_success_execute_record(
                    build_account(),
                    _input({"zero": 0, "short": -0.25}),
                    result,
                    execution_id="semantic-result",
                    execution_kind=ExecutionKind(kind),
                    session_factory=session_factory,
                )
            else:
                record = await records.append_error_execute_record(
                    account_id=1,
                    raw_input=_input({"zero": 0, "short": -0.25}),
                    raw_result=result,
                    msg=str(result["error"]),
                    execution_id="semantic-result",
                    session_factory=session_factory,
                )
            async with factory() as session:
                persisted = await session.get(ExecuteRecord, record.id)
                assert persisted is not None
                reconstructed = await read_execution_result(session, persisted)
                assert business_evidence(persisted.status) == business_evidence(output_status)
                assert business_evidence(persisted.outcome) == business_evidence(outcome)
                assert business_evidence(persisted.is_success) == business_evidence(is_success)
                assert business_evidence(reconstructed["status"]) == business_evidence(output_status)
                assert business_evidence(reconstructed["outcome"]) == business_evidence(outcome)
                assert reconstructed["success"] is bool(is_success)
                assert business_evidence(reconstructed["execution_time"]) == business_evidence(0)
                assert business_evidence(reconstructed["execution_kind"]) == business_evidence(kind)
                assert business_evidence(lifecycle._resolve_completion_status(persisted)) == business_evidence(
                    ExecutionTaskStatus(task_status)
                )
                replay = registry._build_persisted_execution_status(persisted)
                assert business_evidence(replay["status"]) == business_evidence(task_status)
                assert business_evidence(replay["output_status"]) == business_evidence(output_status)
                assert business_evidence(replay["outcome"]) == business_evidence(outcome)
                assert business_evidence(replay["execution_kind"]) == business_evidence(kind)
                assert business_evidence(replay["is_success"]) == business_evidence(is_success)
                if session_scope:
                    assert reconstructed["symbol_results"]["closed"]["final_volume"] is None
                    assert business_evidence(
                        reconstructed["symbol_results"]["closed"]["target_volume"]
                    ) == business_evidence(0)

    asyncio.run(scenario())


@pytest.mark.parametrize("output_status", ["PARTIAL", "BLOCKED", None])
def test_termination_preserves_original_output_and_cancellation_evidence(output_status: str | None) -> None:
    """人工终止与输出进展分别保存，撤单身份及其原顺序不能被去重。"""

    async def scenario() -> None:
        async with _database() as factory:
            raw_result = {"success": False, "execution_time": 0}
            if output_status is not None:
                raw_result["status"] = output_status
            if output_status == "BLOCKED":
                raw_result.update(_session_closed_result(partial=False))
            record = await records.append_terminated_execute_record(
                account_id=1,
                execution_id="semantic-terminated",
                execution_kind=ExecutionKind.CLEAR_POSITIONS,
                reason="人工停止",
                mode=ExecutionTerminateMode.GRACEFUL,
                requested_at="2026-10-20T10:01:00.123+08:00",
                acked_at=None,
                finished_at="2026-10-20T10:01:01.456+08:00",
                cancel_attempted=True,
                cancel_failed_order_ids=["same", "", "same"],
                forced=False,
                cancel_unconfirmed=True,
                raw_input=_input({}),
                raw_result=raw_result,
                session_factory=cast("records.SessionFactory", factory),
            )
            async with factory() as session:
                persisted = await session.get(ExecuteRecord, record.id)
                assert persisted is not None
                result = await read_execution_result(session, persisted)
                assert business_evidence(persisted.is_success) == business_evidence(0)
                assert business_evidence(result.get("status")) == business_evidence(output_status)
                if output_status is None:
                    assert "status" not in result
                assert business_evidence(result["task_status"]) == business_evidence("TERMINATED")
                assert business_evidence(result["outcome"]) == business_evidence("terminated")
                assert business_evidence(result["termination"]["cancel_failed_order_ids"]) == business_evidence(
                    ["same", "", "same"]
                )
                assert result["termination"].get("acked_at") is None
                assert result["termination"]["cancel_unconfirmed"] is True
                replay = registry._build_persisted_execution_status(persisted)
                assert business_evidence(replay["status"]) == business_evidence("TERMINATED")
                assert business_evidence(replay["output_status"]) == business_evidence(output_status)
                assert business_evidence(replay["outcome"]) == business_evidence("terminated")
                assert business_evidence(replay["finished_at"]) == business_evidence("2026-10-20T10:01:01.456+08:00")
                assert business_evidence(replay["cancel_requested_at"]) == business_evidence(
                    "2026-10-20T10:01:00.123+08:00"
                )
                assert replay["error"] is None

    asyncio.run(scenario())


def test_last_target_selects_latest_success_for_exact_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败、其他账户及非 1 成功标志都不能取代最新成功记录的目标。"""

    async def scenario() -> None:
        async with _database() as factory:
            monkeypatch.setattr(rebalance, "SessionLocal", factory)
            for ordinal, account_id, is_success, target in [
                (1, 1, 1, {"older": 0.5}),
                (2, 1, 0, {"failed": 0.9}),
                (3, 1, 1, {"short": -0.25, "zero": 0, "fraction": 0.125}),
                (4, 1, 0, {"newest_failed": 1}),
                (5, 2, 1, {"other_account": 1}),
                (6, 1, 2, {"not_success_one": 1}),
            ]:
                await records._persist_execute_record(
                    account_id=account_id,
                    execution_id=f"semantic-target-{ordinal}",
                    raw_input=_input(target),
                    raw_result={"status": "SUCCEEDED" if is_success == 1 else "FAILED"},
                    is_success=is_success,
                    session_factory=cast("records.SessionFactory", factory),
                )
            assert business_evidence(
                await rebalance._load_last_target_snapshot(build_account(id=1))
            ) == business_evidence(
                {
                    "short": -0.25,
                    "zero": 0,
                    "fraction": 0.125,
                }
            )
            assert business_evidence(
                await rebalance._load_last_target_snapshot(build_account(id=2))
            ) == business_evidence({"other_account": 1})
            assert business_evidence(
                await rebalance._load_last_target_snapshot(build_account(id=999))
            ) == business_evidence({})
            await records.append_success_execute_record(
                build_account(),
                _input({"noop_target": 0}),
                {"status": "NOOP", "success": True},
                execution_id="semantic-latest-noop",
                execution_kind=ExecutionKind.REBALANCE,
                session_factory=cast("records.SessionFactory", factory),
            )
            assert business_evidence(await rebalance._load_last_target_snapshot(build_account())) == business_evidence(
                {"noop_target": 0}
            )
            await records.append_success_execute_record(
                build_account(),
                _input({}),
                {"status": "SUCCEEDED", "success": True},
                execution_id="semantic-latest-clear",
                execution_kind=ExecutionKind.CLEAR_POSITIONS,
                session_factory=cast("records.SessionFactory", factory),
            )
            assert business_evidence(await rebalance._load_last_target_snapshot(build_account())) == business_evidence(
                {}
            )

    asyncio.run(scenario())


def test_mutated_output_status_rederives_outcome_before_real_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型构造后状态被更新时，落库仍以当前状态决定结论，避免错误旧结论残留。"""

    async def scenario() -> None:
        async with _database() as factory:
            monkeypatch.setattr(
                execution_records_output,
                "append_error_execute_record",
                partial(records.append_error_execute_record, session_factory=cast("records.SessionFactory", factory)),
            )
            output = UnifiedStandardOutput(
                status=ExecutionStatus.FAILED,
                error="剩余一手",
                account_assets=UnifiedAccountAssets.unavailable(),
                channel_type=TradeChannel.TQ,
            )
            assert business_evidence(output.outcome) == business_evidence("error")
            output.status = ExecutionStatus.PARTIAL
            record = await execution_records_output.append_execute_record_from_output(
                account=build_account(),
                raw_input=_input({}),
                result=output.model_dump(mode="json"),
                output=output,
                execution_id="semantic-mutated-output",
                execution_kind=ExecutionKind.CLEAR_POSITIONS,
            )
            async with factory() as session:
                persisted = await session.get(ExecuteRecord, record.id)
                assert persisted is not None
                result = await read_execution_result(session, persisted)
                assert business_evidence(result["status"]) == business_evidence("PARTIAL")
                assert business_evidence(result["outcome"]) == business_evidence("not_reached")
                assert business_evidence(result["execution_kind"]) == business_evidence("clear_positions")
                assert business_evidence(persisted.is_success) == business_evidence(0)
                assert business_evidence(
                    registry._build_persisted_execution_status(persisted)["status"]
                ) == business_evidence("FAILED")

    asyncio.run(scenario())


@pytest.mark.parametrize("latest_input", [{"curr_target": {}}, {}, {"curr_target": None}])
def test_latest_success_target_does_not_fall_back_to_older_nonempty_target(
    monkeypatch: pytest.MonkeyPatch, latest_input: dict[str, object]
) -> None:
    """空目标、字段缺失和显式 null 保持原义，不回捞旧持仓意图。"""

    async def scenario() -> None:
        async with _database() as factory:
            monkeypatch.setattr(rebalance, "SessionLocal", factory)
            for ordinal, payload in enumerate([_input({"must_not_restore": 1}), latest_input]):
                await records._persist_execute_record(
                    account_id=1,
                    execution_id=f"semantic-empty-target-{ordinal}",
                    raw_input=payload,
                    raw_result={"status": "SUCCEEDED"},
                    is_success=1,
                    session_factory=cast("records.SessionFactory", factory),
                )
            assert business_evidence(await rebalance._load_last_target_snapshot(build_account())) == business_evidence(
                latest_input.get("curr_target", {})
            )

    asyncio.run(scenario())


def test_committed_input_and_before_assets_survive_without_terminal_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """在最终结果产生前中断时，独立输入和执行前观测仍可从新会话读取。"""

    async def scenario() -> None:
        async with _file_database(monkeypatch, tmp_path, "semantic-crash") as factory:
            input_payload = _input({"zero": 0, "short": -0.25})
            before = {
                "available_cash": 0,
                "total_asset": 120.5,
                "market_value": 120.5,
                "source": "real",
                "update_time": "2026-10-20T10:00:00.123+08:00",
                "positions": [{"symbol": "short", "volume": -2, "available_volume": 0}],
            }
            input_id = await input_persistence._persist_pre_execute_input(
                account_id=1,
                execution_id="semantic-crash-before-result",
                payload=input_payload,
                before_account_assets=before,
            )
            async with factory() as fresh_session:
                assert business_evidence(
                    await fresh_session.scalar(select(func.count()).select_from(ExecuteRecord))
                ) == business_evidence(0)
                assert business_evidence(await read_execution_input(fresh_session, input_id)) == business_evidence(
                    input_payload
                )
                observed = (
                    (
                        await fresh_session.execute(
                            select(assets.asset_snapshot).where(assets.asset_snapshot.c.input_id == input_id)
                        )
                    )
                    .mappings()
                    .one()
                )
                assert observed["record_id"] is None
                assert business_evidence(observed["path"]) == business_evidence("/before_account_assets")
                assert business_evidence(observed["available_cash"]) == business_evidence(0)
                assert business_evidence(observed["total_asset"]) == business_evidence(120.5)
                assert business_evidence(observed["source"]) == business_evidence("real")
                assert business_evidence(observed["update_time"]) == business_evidence(before["update_time"])

    asyncio.run(scenario())


def test_failed_final_transaction_keeps_previously_committed_input(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """分析事实写入失败时回滚整份终态，已提交的下单前输入继续可查。"""

    async def fail_source_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic source write failure")

    async def scenario() -> None:
        async with _file_database(monkeypatch, tmp_path, "semantic-rollback") as factory:
            input_payload = _input({"one": 0.5})
            input_id = await input_persistence._persist_pre_execute_input(
                account_id=1,
                execution_id="semantic-result-rollback",
                payload=input_payload,
            )
            monkeypatch.setattr(records, "write_source_facts", fail_source_write)
            with pytest.raises(RuntimeError, match="synthetic source write failure"):
                await records.append_success_execute_record(
                    build_account(),
                    input_payload,
                    {"status": "SUCCEEDED", "success": True, "symbol_results": {}},
                    execution_id="semantic-result-rollback",
                    session_factory=cast("records.SessionFactory", factory),
                )
            async with factory() as fresh_session:
                assert business_evidence(
                    await fresh_session.scalar(select(func.count()).select_from(ExecuteRecord))
                ) == business_evidence(0)
                assert business_evidence(await read_execution_input(fresh_session, input_id)) == business_evidence(
                    input_payload
                )

    asyncio.run(scenario())
