"""以同一组场景校验算法、汇总、SQLite 回放和前端消费的证据契约。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType
from axile.executor.algorithms.utils.final_position import read_final_position
from axile.executor.algorithms.utils.outcome import summarize_outcome
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.models.execution_result import AlgorithmResult
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.server.db.models import ExecuteRecord, ExecutionArtifact
from axile.server.execution import lifecycle
from axile.server.execution.backend import _dump_output_result
from axile.server.execution.worker_backend.worker_responses import _dump_output_payload
from axile.server.performance_analysis import _events
from axile.server.performance_costs import project_execution
from tests.unit.executor.test_execution_engine_lifecycle import _standard_input

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
    )


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

    db = create_engine("sqlite://")
    ExecuteRecord.__table__.create(db)
    ExecutionArtifact.__table__.create(db)

    async def persist_artifact(**values):
        artifact = ExecutionArtifact(**values)
        with db.begin() as conn:
            conn.execute(ExecutionArtifact.__table__.insert().values(**artifact.model_dump(exclude={"id"})))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(lifecycle, "append_execution_artifact", persist_artifact)
            asyncio.run(
                lifecycle.append_execution_result_artifacts(
                    execution_id="contract",
                    result=worker_raw,
                    before_account_assets=_assets(inputs["before"]).model_dump(mode="json"),
                )
            )
        with db.begin() as conn:
            conn.execute(
                ExecuteRecord.__table__.insert().values(
                    account_id=1,
                    execution_id="contract",
                    raw_input={},
                    raw_result=worker_raw,
                    is_success=int(output.success),
                    created_at="2026-09-15T10:00:00",
                )
            )
            stored = conn.execute(select(ExecuteRecord.__table__)).mappings().one()
            record = SimpleNamespace(**stored)
            summary = conn.execute(
                select(ExecutionArtifact.__table__.c.content).where(
                    ExecutionArtifact.__table__.c.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY,
                )
            ).scalar_one()
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
    finally:
        db.dispose()


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
