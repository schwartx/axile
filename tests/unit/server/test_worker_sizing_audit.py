"""Worker 执行路径的目标数量换算审计契约测试."""

from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from axile.domain.execution import ExecutionArtifactType
from axile.executor.models.execution_result import TargetSizingDecision
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.server.execution.execution_summaries import execution_result_view
from axile.server.execution.input_persistence import sanitize_execution_input
from axile.server.execution.worker_backend import worker_audit
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import seed_account
from tests.unit.server._execution_test_support import FakeExecutor, build_account, capture_worker_audit_batch
from tests.unit.server._native_read_views import read_view


def test_worker_target_reference_and_derived_detail_keep_sizing(
    native_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标使用不可变引用，结果事实按需组装换算证据。"""
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    seed_account(native_database)
    account = build_account()
    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": str(account.trade_channel),
            "account_config": account.account_config,
            "curr_target": {"m2701": -0.18},
            "last_target": {},
        }
    )
    executor = FakeExecutor()
    output = cast(UnifiedStandardOutput, executor.execute(standard_input))
    symbol_result = output.symbol_results["ag2612"]
    symbol_result.sizing = TargetSizingDecision(
        symbol="ag2612",
        reason_code="COMMON.SIZING.QUANTIZED",
        account_weight=0.1,
        equity=1_000.0,
        reference_price=180.0,
        unit_multiplier=1.0,
        unit_notional=180.0,
        target_notional=100.0,
        raw_quantity=0.555555,
        target_quantity=0.5,
        quantity_step=0.1,
    )
    result = cast("dict[str, object]", output.model_dump(mode="json"))
    artifacts: list[dict[str, object]] = []

    def capture_artifact(**kwargs: object) -> bool:
        artifacts.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        worker_audit, "append_execution_audit_batch_sync", capture_worker_audit_batch(artifact=capture_artifact)
    )
    persist_input = Mock(wraps=worker_audit.persist_pre_execute_input_sync)
    monkeypatch.setattr(worker_audit, "persist_pre_execute_input_sync", persist_input)
    audit_input = {
        **sanitize_execution_input(standard_input.to_dict()),
        "strategy_target": {"m2701": -0.06},
        "sizing_context": {
            "long_leverage": 1.0,
            "short_leverage": 3.0,
            "weight_precision": 0.01,
        },
    }

    worker_audit._append_trade_pre_execute_audit(
        account=account,
        execution_id="exec-worker-sizing",
        algorithm_name="SINGLE-MAKER",
        trigger_source="manual",
        audit_input=audit_input,
        standard_input=standard_input,
        executor=executor,
    )
    worker_audit._append_success_audit(
        account=account,
        execution_id="exec-worker-sizing",
        algorithm_name="SINGLE-MAKER",
        output=output,
        result=result,
        executor=executor,
    )

    standard = next(
        artifact for artifact in artifacts if artifact["artifact_type"] == ExecutionArtifactType.STANDARD_INPUT
    )
    target = next(
        artifact for artifact in artifacts if artifact["artifact_type"] == ExecutionArtifactType.TARGET_SNAPSHOT
    )
    assert all(a["artifact_type"] != "execution_summary" for a in artifacts)
    input_id = cast("dict[str, dict[str, object]]", standard["content"])["business_facts_ref"]["input_id"]
    persist_input.assert_called_once_with(
        account_id=account.id,
        execution_id="exec-worker-sizing",
        payload=audit_input,
        before_account_assets=None,
    )
    assert standard["schema_version"] == target["schema_version"] == 3
    assert standard["content"] == {"business_facts_ref": {"version": 1, "view": "input", "input_id": input_id}}
    assert target["content"] == {"business_facts_ref": {"version": 1, "view": "targets", "input_id": input_id}}
    frozen_input = read_view(native_database, "read_execution_input", {"input_id": input_id})
    assert frozen_input["strategy_target"] == {"m2701": -0.06}
    assert frozen_input["curr_target"] == {"m2701": -0.18}
    assert "account_target" not in frozen_input
    assert frozen_input["sizing_context"] == {
        "long_leverage": 1.0,
        "short_leverage": 3.0,
        "weight_precision": 0.01,
    }
    detail = native_store_call(
        native_database,
        "execution_read",
        {"op": "detail", "execution_id": "exec-worker-sizing", "account_id": account.id},
    )
    reconciliation = execution_result_view(detail["result"])["reconciliation"]
    symbols = cast("list[dict[str, object]]", reconciliation["symbols"])
    assert symbols[0]["sizing"] == symbol_result.sizing.model_dump(mode="json")
