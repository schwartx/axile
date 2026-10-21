"""Worker 执行路径的目标数量换算审计契约测试."""

from typing import cast

import pytest

from axile.domain.execution import ExecutionArtifactType
from axile.executor.models.execution_result import TargetSizingDecision
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.server.execution.worker_backend import worker_audit
from tests.unit.server._execution_test_support import FakeExecutor, build_account, capture_worker_audit_batch


def test_worker_target_reference_and_summary_keep_sizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标使用不可变引用，执行摘要保留完整的 v2 换算证据。"""
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
    frozen_inputs: list[dict[str, object]] = []
    audit_variants: list[dict[str, object]] = []

    def capture_artifact(**kwargs: object) -> bool:
        artifacts.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        worker_audit, "append_execution_audit_batch_sync", capture_worker_audit_batch(artifact=capture_artifact)
    )
    monkeypatch.setattr(worker_audit, "append_execution_event_sync", lambda **_kwargs: True)

    def capture_input(**kwargs: object) -> tuple[str, str]:
        frozen_inputs.append(cast("dict[str, object]", kwargs["payload"]))
        audit_variants.append(cast("dict[str, object]", kwargs["audit_payload"]))
        return "worker-sizing-input", "worker-sizing-audit"

    monkeypatch.setattr(worker_audit, "persist_pre_execute_bundle_sync", capture_input)
    monkeypatch.setattr(
        worker_audit, "persist_post_execute_assets_sync", lambda **_kwargs: ("worker-sizing-input", "after-assets")
    )

    worker_audit._append_trade_pre_execute_audit(
        account=account,
        execution_id="exec-worker-sizing",
        algorithm_name="SINGLE-MAKER",
        trigger_source="manual",
        audit_input={
            "strategy_target": {"m2701": -0.06},
            "sizing_context": {
                "long_leverage": 1.0,
                "short_leverage": 3.0,
                "weight_precision": 0.01,
            },
        },
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

    target = next(
        artifact for artifact in artifacts if artifact["artifact_type"] == ExecutionArtifactType.TARGET_SNAPSHOT
    )
    summary = next(
        artifact for artifact in artifacts if artifact["artifact_type"] == ExecutionArtifactType.EXECUTION_SUMMARY
    )
    assert target["schema_version"] == 3
    assert target["content"] == {
        "business_facts_ref": {"version": 1, "view": "targets", "input_id": "worker-sizing-audit"}
    }
    assert frozen_inputs[0]["strategy_target"] == {"m2701": -0.06}
    assert "account_target" not in frozen_inputs[0]
    assert "curr_target" not in frozen_inputs[0]
    assert audit_variants[0]["account_target"] == {"m2701": -0.18}
    assert summary["schema_version"] == 2
    reconciliation = cast("dict[str, object]", cast("dict[str, object]", summary["content"])["reconciliation"])
    symbols = cast("list[dict[str, object]]", reconciliation["symbols"])
    assert symbols[0]["sizing"] == symbol_result.sizing.model_dump(mode="json")
