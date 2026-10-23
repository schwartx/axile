"""以同一组场景校验算法、汇总、SQLite 回放和前端消费的证据契约。"""

import asyncio
import json
import pathlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType, ExecutionEventType
from axile.executor.algorithms.utils.final_position import read_final_position
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.models.execution_result import AlgorithmResult
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.session_closed import COMMON_SESSION_CLOSED
from axile.server.business_facts.artifacts import parse_artifact_reference, resolve_artifact_content
from axile.server.business_facts.assets import input_after_asset_fact_id, input_asset_fact_id
from axile.server.business_facts.repository import (
    persist_execution_input,
    read_execution_result,
    store_execution_facts,
)
from axile.server.db.models import ExecuteRecord, ExecutionArtifact
from axile.server.execution import input_persistence
from axile.server.execution.backend import _dump_output_result
from axile.server.execution.worker_backend import worker_audit
from axile.server.execution.worker_backend.worker_responses import _dump_output_payload
from axile.server.performance_costs import project_execution
from scripts.business_facts_contract import business_evidence
from tests.unit.executor.test_execution_engine_lifecycle import _standard_input
from tests.unit.server._business_analysis_support import _events
from tests.unit.server._execution_test_support import capture_worker_audit_batch

FIXTURE = Path(__file__).parents[2] / "fixtures" / "execution_evidence_contract.json"


def _load_cases():
    if not FIXTURE.is_file():
        raise FileNotFoundError(f"缺少证据契约 fixture: {FIXTURE}")
    return json.loads(FIXTURE.read_text())


CASES = _load_cases()


def _assets(spec):
    volume = spec["volume"]
    return UnifiedAccountAssets(
        available_cash=1000,
        total_asset=1000,
        market_value=abs(volume) * 100,
        source=spec["source"],
        positions=[
            Position(
                symbol="A",
                volume=abs(volume),
                available_volume=abs(volume),
                market_value=abs(volume) * 100,
                direction=PositionDirection.SHORT if volume < 0 else PositionDirection.LONG,
            )
        ]
        if volume
        else [],
        update_time="2026-09-15T10:00:00",
    )


def _snapshot_reader(spec):
    if spec["source"] == "query_failed":
        return Mock(side_effect=RuntimeError("synthetic query failure"))
    return Mock(return_value=_assets(spec))


def _quantity(assets):
    return sum(-p.volume if p.direction == PositionDirection.SHORT else p.volume for p in assets.positions)


def _algorithm_result(case):
    inputs = case["input"]
    reader = Mock(get_account_assets=_snapshot_reader(inputs["algorithm"]))
    assets, final, query_error = read_final_position(reader, _quantity)
    order = inputs["order"]
    orders = (
        []
        if order is None
        else [
            UnifiedOrder(
                order_id="contract-order",
                symbol="A",
                direction=OrderDirection.BUY,
                order_type=OrderType.LIMIT,
                volume=2,
                price=100,
                status=order["status"],
                filled_volume=order["filled"],
            )
        ]
    )
    return AlgorithmResult(
        **summarize_outcome(
            inputs["before"]["volume"],
            final,
            inputs["target"],
            orders,
            [],
            explicit_error=query_error or inputs.get("error"),
            explicit_blocked_error=inputs.get("blocked"),
        ),
        symbol="A",
        algorithm="contract",
        orders=orders,
        target_volume=inputs["target"],
        account_assets=assets,
        reason_code=COMMON_SESSION_CLOSED if inputs.get("blocked") else None,
    )


async def _persist_contract_result(output, worker_raw, before_assets, monkeypatch):
    """实际写入类型化事实和 v3 附件，再从唯一业务读取路径回放。"""
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "contract.db"
        # 执行后观测经原生入口冻结,须指向同一测试文件。
        monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
        db = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with db.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with async_sessionmaker(db, class_=AsyncSession, expire_on_commit=False)() as session:
                input_id = await persist_execution_input(
                    session,
                    account_id=1,
                    execution_id="contract",
                    payload={},
                    created_at="2026-09-15T10:00:00",
                    before_assets=before_assets,
                )
                await session.commit()
                after_reference = await input_persistence._persist_post_execute_assets(
                    account_id=1, execution_id="contract", payload=worker_raw
                )
                artifacts = []
                post_writer = Mock(return_value=after_reference)
                with monkeypatch.context() as patch:
                    patch.setattr(
                        worker_audit,
                        "append_execution_audit_batch_sync",
                        capture_worker_audit_batch(
                            artifact=lambda **values: artifacts.append(ExecutionArtifact(**values))
                        ),
                    )
                    patch.setattr(worker_audit, "append_execution_event_sync", lambda **kwargs: None)
                    patch.setattr(worker_audit, "persist_post_execute_assets_sync", post_writer)
                    worker_audit._append_success_audit(
                        account=SimpleNamespace(id=1, trade_channel=TradeChannel.CTP),
                        execution_id="contract",
                        algorithm_name="contract",
                        output=output,
                        result=worker_raw,
                        executor=object(),
                        before_account_assets=before_assets,
                    )
                post_writer.assert_called_once_with(account_id=1, execution_id="contract", payload=worker_raw)
                record = ExecuteRecord(
                    account_id=1,
                    execution_id="contract",
                    is_success=int(output.success),
                    created_at="2026-09-15T10:00:00",
                )
                session.add(record)
                await session.flush()
                await store_execution_facts(session, record, raw_input={}, raw_result=worker_raw)
                session.add_all(artifacts)
                await session.commit()
                assert record.input_id == input_id
                result = await read_execution_result(session, record)
                assert business_evidence(result) == business_evidence(worker_raw)
                stored_artifacts = (await session.scalars(select(ExecutionArtifact))).all()
                after = next(
                    item for item in stored_artifacts if item.artifact_type == ExecutionArtifactType.ACCOUNT_SNAPSHOT
                )
                assert after.schema_version == 3
                assert parse_artifact_reference(after.content) == {
                    "version": 1,
                    "view": "assets_after",
                    "input_id": input_id,
                    "asset_id": input_after_asset_fact_id(input_id),
                }
                assert "account_assets" not in after.content
                resolved = await resolve_artifact_content(session, after, account_id=1)
                assert business_evidence(resolved["account_assets"]) == business_evidence(worker_raw["account_assets"])
                summary = next(
                    item for item in stored_artifacts if item.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY
                )
                assert summary.schema_version == 2
                assert "raw_input" not in ExecuteRecord.__table__.c
                assert "raw_result" not in ExecuteRecord.__table__.c
                return SimpleNamespace(
                    id=record.id,
                    execution_id=record.execution_id,
                    created_at=record.created_at,
                    is_success=record.is_success,
                    result=result,
                ), summary.content
        finally:
            await db.dispose()


def build_contract_wire(case, monkeypatch):
    """实际生成、落库并读回结果；fixture 只保留跨层消费的字段，避免时间戳噪声。"""
    inputs = case["input"]
    owner = Mock(channel_type=TradeChannel.CTP, get_account_assets=_snapshot_reader(inputs["after"]))
    runtime = Mock(memory={}, elapsed_seconds=Mock(return_value=1))
    output = ExecutionEngine(owner, runtime)._create_standard_output_from_results(
        _standard_input(),
        [_algorithm_result(case)],
    )
    raw = _dump_output_result(output)
    worker_raw = _dump_output_payload(output)
    assert {k: v for k, v in raw.items() if k != "inputs"} == worker_raw
    json.dumps(worker_raw, allow_nan=False)
    record, summary = asyncio.run(
        _persist_contract_result(output, worker_raw, _assets(inputs["before"]).model_dump(mode="json"), monkeypatch)
    )
    projected, _ = project_execution(record)
    events = _events([record], [], [])
    result_keys = ("status", "error", "target_volume", "final_volume")
    row_keys = ("symbol", "before", "after", "target", "reached", "attained_ratio", "moved", "drift")
    return {
        "summary": {
            **{key: summary[key] for key in ("status", "error", "success")},
            "symbol_results": {"A": {key: summary["symbol_results"]["A"][key] for key in result_keys}},
            "reconciliation": {
                "account": summary["reconciliation"]["account"],
                "symbols": [{key: summary["reconciliation"]["symbols"][0][key] for key in row_keys}],
            },
        },
        "list_result": projected["record"]["raw_result"],
        "event_label": events[0]["tag"] if events else None,
    }


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_execution_evidence_contract(case, monkeypatch):
    wire = build_contract_wire(case, monkeypatch)
    expected = case["expected"]
    summary = wire["summary"]
    row = summary["reconciliation"]["symbols"][0]
    assert summary["status"] == expected["status"]
    assert summary["symbol_results"]["A"]["status"] == expected["symbol_status"]
    assert row["after"] == expected["observed_after"]
    assert row["reached"] == expected["reached"]
    assert summary["reconciliation"]["account"]["equity_after"] == expected["equity_after"]
    assert wire == case["wire"], "后端结果与前端共用 fixture 不一致；需一起审查契约变更"


@pytest.mark.parametrize("execution_kind", ["rebalance", "clear_positions"])
@pytest.mark.parametrize("has_before_assets", [False, True])
def test_pre_execute_audit_references_committed_input_and_preserves_missing_assets(
    execution_kind, has_before_assets, monkeypatch
):
    """调仓与清仓引用已提交事实；缺少前资产时保持明确的不可用证据。"""
    trace = []
    artifacts = []
    events = []
    standard_input = _standard_input()
    audit_input = {"strategy_target": {"rb2610": 0.05}, "execution_kind": execution_kind}
    before = _assets({"source": "real", "volume": 0}).model_dump(mode="json") if has_before_assets else None

    def persist_input(**values):
        trace.append("input_committed")
        assert values["account_id"] == 1
        assert values["execution_id"] == "contract-pre"
        assert values["before_account_assets"] is before
        assert values["payload"] == audit_input
        return "committed-input"

    def append_artifact(**values):
        trace.append("artifact")
        artifacts.append(values)

    def append_event(**values):
        trace.append("event")
        events.append(values)

    monkeypatch.setattr(worker_audit, "persist_pre_execute_input_sync", persist_input)
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        capture_worker_audit_batch(artifact=append_artifact, event=append_event),
    )
    monkeypatch.setattr(worker_audit, "execution_environment", lambda *_args: {"synthetic": True})
    common = {
        "account": SimpleNamespace(id=1, trade_channel=TradeChannel.CTP),
        "execution_id": "contract-pre",
        "algorithm_name": "contract",
        "executor": object(),
        "audit_input": audit_input,
        "before_account_assets": before,
    }
    if execution_kind == "rebalance":
        worker_audit._append_trade_pre_execute_audit(**common, trigger_source="manual", standard_input=standard_input)
    else:
        worker_audit._append_empty_positions_pre_execute_audit(**common)

    assert trace[0] == "input_committed"
    assert trace.count("input_committed") == 1
    by_type = {artifact["artifact_type"]: artifact for artifact in artifacts}
    input_artifact = by_type[ExecutionArtifactType.STANDARD_INPUT]
    assert input_artifact["schema_version"] == 3
    assert input_artifact["content"] == {
        "business_facts_ref": {"version": 1, "view": "input", "input_id": "committed-input"}
    }
    before_artifact = by_type[ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE]
    if has_before_assets:
        assert before_artifact["schema_version"] == 3
        assert before_artifact["content"] == {
            "business_facts_ref": {
                "version": 1,
                "view": "assets_before",
                "input_id": "committed-input",
                "asset_id": input_asset_fact_id("committed-input"),
            },
            "source": "real",
        }
    else:
        assert before_artifact["schema_version"] == 1
        assert before_artifact["content"] == {"account_assets": {}, "source": "unavailable"}
    if execution_kind == "rebalance":
        for kind, view in (
            (ExecutionArtifactType.TARGET_SNAPSHOT, "targets"),
            (ExecutionArtifactType.TRADE_RULES_SNAPSHOT, "rules"),
        ):
            assert by_type[kind]["schema_version"] == 3
            assert by_type[kind]["content"] == {
                "business_facts_ref": {"version": 1, "view": view, "input_id": "committed-input"}
            }
        target_event = next(event for event in events if event["event_type"] == ExecutionEventType.TARGET_COMPUTED)
        assert target_event["details"]["decision"] == {
            "business_facts_ref": {
                "version": 1,
                "view": "targets",
                "input_id": "committed-input",
                "select_fields": ["curr_target"],
            },
        }
    else:
        assert ExecutionArtifactType.TARGET_SNAPSHOT not in by_type
        assert ExecutionArtifactType.TRADE_RULES_SNAPSHOT not in by_type
    started = next(event for event in events if event["event_type"] == ExecutionEventType.EXECUTION_STARTED)
    assert started["details"]["debug"]["execution_kind"] == execution_kind
