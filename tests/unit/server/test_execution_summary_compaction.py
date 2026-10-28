"""worker 回传前保存原生事实，附件不再承载结果 JSON。"""

from types import SimpleNamespace

from axile.domain.execution import ExecutionArtifactType
from axile.executor.models.execution_result import ExecutionStatus
from axile.server.execution.worker_backend import worker_audit
from tests.unit.server._execution_test_support import build_account, capture_worker_audit_batch


def test_worker_journal_keeps_native_orders_until_terminal_facts_commit(monkeypatch):
    artifacts = []
    staged = []
    monkeypatch.setattr(worker_audit, "stage_execute_result_sync", lambda **kwargs: staged.append(kwargs))
    result = {
        "status": "SUCCEEDED",
        "success": True,
        "account_assets": {"total_asset": 10.0, "positions": []},
        "symbol_results": {
            "A": {
                "status": "SUCCEEDED",
                "target_volume": 1.0,
                "final_volume": 1.0,
                "orders": [{"order_id": "journal-order", "filled_volume": 1.0, "avg_price": 10.0}],
                "trades": [{"trade_id": "journal-trade", "order_id": "journal-order", "trade_volume": 1.0}],
            }
        },
    }
    monkeypatch.setattr(worker_audit, "persist_post_execute_assets_sync", lambda **_kwargs: ("input-1", "asset-1"))
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        capture_worker_audit_batch(artifact=lambda **kwargs: artifacts.append(kwargs)),
    )
    monkeypatch.setattr(worker_audit, "append_execution_event_sync", lambda **_kwargs: None)
    worker_audit._append_success_audit(
        account=build_account(),
        execution_id="pending-journal",
        algorithm_name="SINGLE-MAKER",
        output=SimpleNamespace(status=ExecutionStatus.SUCCEEDED, success=True),
        result=result,
        executor=object(),
    )
    assert staged[0]["result"] == result
    assert all(item["artifact_type"] != ExecutionArtifactType.EXECUTION_SUMMARY for item in artifacts)
